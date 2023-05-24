import sqlite3
import sys
from contextlib import closing

import functools
import threading
import argparse
import json
import rure
from typing import Literal


class ParsedArgs(argparse.Namespace):
    db_name: str
    type: Literal["public"] | Literal["private"]


# Below taken from datasette-rure so we can use sqlite regexes when building our sqlite file
@functools.lru_cache(maxsize=128)
def _compiled_regex(threadid, pattern):
    return rure.compile(pattern)


def compiled_regex(pattern):
    return _compiled_regex(threading.get_ident(), pattern)


def none_on_exception(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    return inner


@none_on_exception
def regexp(pattern, input):
    return 1 if compiled_regex(pattern).search(input) else 0


@none_on_exception
def regexp_match(pattern, input, index=1):
    return compiled_regex(pattern).match(input).group(index)


@none_on_exception
def regexp_matches(pattern, input):
    return json.dumps([m.groupdict() for m in compiled_regex(pattern).finditer(input)])


def postprocess(parsed_args: ParsedArgs):
    if parsed_args.type == "private":
        print("WARNING: running private export, handle the export with care")
    with closing(sqlite3.connect(parsed_args.db_name)) as connection:
        connection.create_function("regexp", 2, regexp)
        connection.create_function("regexp_match", 2, regexp_match)
        connection.create_function("regexp_match", 3, regexp_match)
        connection.create_function("regexp_matches", 2, regexp_matches)
        with closing(connection.cursor()) as cursor:
            if parsed_args.type == "public":
                print("Ensuring no passwords are published")
                cursor.execute("UPDATE users set password=''")

                print("Mask out email addresses of non-owid emails")
                cursor.execute(
                    "UPDATE users SET email=((replace(fullName, ' ', '.') || '@former-contributor.org')) WHERE email NOT LIKE '%ourworldindata.org'"
                )

                print("Remove all posts that are not published (draft, private)")
                cursor.execute("DELETE FROM posts WHERE status!='publish'")

                print("Remove all posts_gdocs that are not published")
                cursor.execute("DELETE FROM posts_gdocs WHERE published=0")

                print("Dropping confidential table pageviews")
                cursor.execute("DROP TABLE IF EXISTS pageviews")

            print("Create relationship table charts_variables")
            cursor.execute(
                """-- sql
            CREATE table chart_variables
            (
                `chartId` integer NOT NULL,
                `variableId` integer NOT NULL,
                PRIMARY KEY (`chartId`, `variableId`),
                CONSTRAINT `FK_chart_variables_chartId` FOREIGN KEY (`chartId`) REFERENCES `charts` (`id`) ON DELETE CASCADE,
                CONSTRAINT `FK_chart_variables_variableId` FOREIGN KEY (`variableId`) REFERENCES `variables` (`id`) ON DELETE CASCADE
            );"""
            )

            cursor.execute(
                """-- sql
            INSERT INTO chart_variables

            -- get the map variables that are not null
            SELECT
                id as chartId,
                JSON_EXTRACT(config, '$.map.variableId') as variableId
            FROM charts
            WHERE variableId is not null

            UNION

            -- and union it together with all the variables hidden in the dimensions json array, extracted with json_each
            SELECT
                charts.id as chartId,
                JSON_EXTRACT(dimension.value, '$.variableId') as variableId
            from charts,
            json_each(config, '$.dimensions') as dimension;
            """
            )

            print("Add useful columns to charts")
            cursor.executescript(
                """-- sql
            ALTER TABLE charts
                ADD COLUMN title TEXT GENERATED ALWAYS as (JSON_EXTRACT(config, '$.title'))  VIRTUAL;
            ALTER TABLE charts
                ADD COLUMN subtitle TEXT GENERATED ALWAYS as (JSON_EXTRACT(config, '$.subtitle'))  VIRTUAL;
            ALTER TABLE charts
                ADD COLUMN note TEXT GENERATED ALWAYS as (JSON_EXTRACT(config, '$.note'))  VIRTUAL;
                """
            )

            print("Add useful columns to sources")
            cursor.executescript(
                """-- sql
            ALTER TABLE sources
                ADD COLUMN additionalInfo TEXT GENERATED ALWAYS as (JSON_EXTRACT(description, '$.additionalInfo')) VIRTUAL;
            ALTER TABLE sources
                ADD COLUMN link TEXT GENERATED ALWAYS as (JSON_EXTRACT(description, '$.link')) VIRTUAL;
            ALTER TABLE sources
                ADD COLUMN dataPublishedBy TEXT GENERATED ALWAYS as (JSON_EXTRACT(description, '$.dataPublishedBy')) VIRTUAL;
                """
            )

            print("Add views for database cleaning")

            # Datasets uploaded/updated/edited more than 1 year ago, and with 0 chart
            cursor.executescript(
                """-- sql
            CREATE VIEW unused_old_datasets
            AS
            SELECT name,
                   printf("https://owid.cloud/admin/datasets/%d", id) as url
            FROM datasets d
            WHERE NOT EXISTS
                (SELECT 1
                FROM chart_variables cv
                JOIN variables v ON cv.variableId = v.id
                WHERE v.datasetId = d.id )
            AND isArchived=0
            AND createdAt <= date('now', '-1 year')
            AND updatedAt <= date('now', '-1 year')
            AND metadataEditedAt <= date('now', '-1 year')
            AND dataEditedAt <= date('now', '-1 year')
            AND description not like '%core-econ%';
                """
            )

            # Charts with no topic page assigned
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_without_origin_url
            AS
            select
                title,
                group_concat(t.name) as tags,
                printf("https://owid.cloud/admin/charts/%d/edit", c.id) AS url
            from
                charts c
                left join chart_tags ct on c.id = ct.chartId
                left join tags t on ct.tagId = t.id
            where
                (
                    trim(json_extract(config, "$.originUrl")) = ""
                    OR json_extract(config, "$.originUrl") IS NULL
                )
                and json_extract(config, "$.isPublished") = True
            group by
                c.title
            order by
                2,
                1
                """
            )

            # Charts that are potential duplicates
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_potential_duplicates
            AS
            WITH chart_list AS (
            SELECT
                chartId,
                group_concat(variableId) AS variables
            FROM
                chart_variables
            GROUP BY
                chartId
            ), charts_per_variable AS (
            SELECT
                variables,
                group_concat(chartId, '%29+%28%3D+charts.id+' ) as chart_ids
            FROM
                chart_list
            GROUP BY
                variables
            HAVING
                COUNT(*) > 1
            ORDER BY
                variables
            )
            select
            'https://owid.cloud/admin/bulk-grapher-config-editor?filter=%28OR+%28%3D+charts.id+' || chart_ids || '%29%29' as compare_link,
            variables
            from charts_per_variable
                """
            )

            # Charts where the originUrl doesn't link to an existing post
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_broken_origin_url
            AS
            WITH chartOriginUrl AS (
            SELECT
                id AS chartId,
                slug AS chartSlug,
                "https://owid.cloud/admin/charts/" || id || "/edit" AS chartEditLink,
                JSON_EXTRACT(config, "$.originUrl") AS originUrlAsAuthored,
                trim(
                    regexp_match(
                        '^.*[Oo]ur[Ww]orld[Ii]n[Dd]ata.org/(.+)$',
                        JSON_EXTRACT(config, "$.originUrl")
                    ),
                    '/'
                ) AS originUrlPostSlug
            FROM
                charts
            WHERE
                originUrlAsAuthored IS NOT NULL
                AND originUrlAsAuthored IS NOT ""
                AND originUrlAsAuthored not like "%tinyco.re%"
            )
            SELECT
            c.chartId,
            c.chartSlug,
            c.chartEditLink,
            c.originUrlAsAuthored,
            c.originUrlPostSlug,
            CASE
                WHEN c.originUrlPostSlug IS NOT NULL
                THEN "https://ourworldindata.org/" || c.originUrlPostSlug
                ELSE NULL
            END AS originUrlPostLink
            FROM
            chartOriginUrl c
            LEFT JOIN posts p ON p.slug = c.originUrlPostSlug
            WHERE
            p.id IS NULL
                """
            )

            # Number of live charts per dataset
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_per_dataset
            AS
            select
                d.id,
                d.name,
                count(distinct chartId) as n_charts
            from
                datasets d
                join variables v on v.datasetId = d.id
                join chart_variables cv on cv.variableId = v.id
                join charts c on cv.chartId = c.id
            where json_extract(c.config, "$.isPublished")
            group by
                d.id,
                d.name
            order by
                n_charts desc
                """
            )

            # Charts & maps set to a manual year
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_with_manual_year
            AS
            select
                id,
                title,
                printf("https://owid.cloud/admin/charts/%s/edit", id) as edit_url
            from
                charts
            where
                json_extract(config, "$.maxTime") is not null
                and json_extract(config, "$.maxTime") != "latest"
                """
            )

            # Charts without any tag
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_without_tag
            AS
            select
                id,
                title,
                "https://owid.cloud/admin/variables/" || variableId as edit_url
            from
                charts c
                join (
                    select
                    chartId,
                    max(variableId) as variableId
                from
                    chart_variables
                    group by
                    chartId
                ) cv on c.id = cv.chartId
            where
                id not in (
                    select
                        chartId
                    from
                        chart_tags
                )
                and json_extract(config, "$.isPublished")
            order by
            id
                """
            )

            # Charts with an identical title, variant included
            cursor.executescript(
                """-- sql
            CREATE VIEW charts_same_title_variant
            AS
            with full_titles as (
            select
                printf(
                "%s (%s)",
                title,
                json_extract(config, "$.variantName")
                ) as full_title,
                id
            from
                charts
            where
                json_extract(config, "$.isPublished")
            )
            select
            a.full_title,
            printf("https://owid.cloud/admin/charts/%s/edit", a.id) as chartA,
            printf("https://owid.cloud/admin/charts/%s/edit", b.id) as chartB
            from
            full_titles a
            join full_titles b on a.full_title = b.full_title
            and a.id < b.id
            order by 1
                """
            )

            if parsed_args.type == "private":
                cursor.executescript(
                    """-- sql
                    CREATE VIEW charts_pageviews
                    AS
                    select id as grapherId, slug, type, config->>"$.isPublished" as isPublished, views_7d, views_14d, views_365d
                    from charts c
                    left join pageviews pv on pv.url = "https://ourworldindata.org/grapher/" || c.slug
                    order by views_14d desc
                    """
                )
                cursor.executescript(
                    """-- sql
                    CREATE VIEW topic_pages_pageviews
                    AS
                    SELECT url,
                        views_365d,
                        iif(content like "%bodyClassName:topic-page%", "new", "old") page_format
                    FROM pageviews pv
                    JOIN posts p ON replace(pv.url, "https://ourworldindata.org/", "") = p.slug
                    WHERE TYPE = "page"
                    AND DAY =
                        (SELECT max(DAY)
                        FROM pageviews)
                    AND slug not in ("", "privacy-policy", "blog", "team", "faqs", "about", "jobs")
                    ORDER BY views_365d DESC
                    """
                )

            connection.commit()
            print("done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db_name", help="The name of the database to postprocess")
    parser.add_argument(
        "-t",
        "--type",
        help="Whether this is a private or public export",
        choices=["private", "public"],
        default="public",
    )
    parsed_args = parser.parse_args(namespace=ParsedArgs())
    postprocess(parsed_args)
