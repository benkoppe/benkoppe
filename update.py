import datetime
from typing import Literal
import requests
from dateutil import relativedelta
from pathlib import Path
import hashlib
from dataclasses import dataclass
import re
import os

# adapted from https://github.com/Andrew6rant/Andrew6rant/blob/main/today.py
# i converted it to a text file, rather than a SVG, and rewrote the code

HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}
USER_NAME = os.environ["USER_NAME"]

BIRTH = {"YEAR": 2003, "MONTH": 11, "DAY": 14}

COMMENT_SIZE = 7  # number of comment lines in cache file


@dataclass
class ReplacementConfig:
    regex: str
    output: str
    fill_char: str


REPLACEMENT_CONFIGS = {
    "age": ReplacementConfig(r"Uptime:.*\n", "Uptime:{filler}{in1}\n", "."),
    "repos": ReplacementConfig(
        r"Repos:.* \|", "Repos:{filler}{in1:,} {{Contributed: {in2:,}}} |", "."
    ),
    "stars": ReplacementConfig(r"Stars:.*\n", "Stars:{filler}{in1:,}\n", "."),
    "commits": ReplacementConfig(r"Commits:.* \|", "Commits:{filler}{in1:,} |", "."),
    "followers": ReplacementConfig(
        r"Followers:.*\n", "Followers:{filler}{in1:,}\n", "."
    ),
    "loc": ReplacementConfig(
        r"Lines of Code on GitHub:.* \(",
        "Lines of Code on GitHub:{filler}{in1:,} (",
        ".",
    ),
    "loc_add_del": ReplacementConfig(
        r"\(.*\n", "( {in1:,}++,{filler}{in2:,}-- )\n", " "
    ),
}


def make_replacement(text: str, config: ReplacementConfig, in1, in2=None):
    def create_new_text(match: re.Match):
        matched_text = match.group(0)
        target_len = len(matched_text)

        output_no_filler = config.output.format(filler="", in1=in1, in2=in2)
        no_filler_len = len(output_no_filler)

        required_filler = target_len - no_filler_len
        filler = ""
        if required_filler <= 2:
            filler += " " * required_filler
        else:
            filler += " " + config.fill_char * (required_filler - 2) + " "

        return config.output.format(filler=filler, in1=in1, in2=in2)

    return re.sub(config.regex, create_new_text, text)


def replace_all(
    text: str,
    age: str,
    repos_owner: int,
    repos_total: int,
    stars: int,
    commits: int,
    followers: int,
    loc_total: int,
    loc_add: int,
    loc_del: int,
):
    curr_text = text

    def replace_single(config_key, in1, in2=None):
        nonlocal curr_text
        curr_text = make_replacement(
            curr_text, REPLACEMENT_CONFIGS[config_key], in1, in2
        )

    replace_single("age", age)
    replace_single("repos", repos_owner, repos_total)
    replace_single("stars", stars)
    replace_single("commits", commits)
    replace_single("followers", followers)
    replace_single("loc", loc_total)
    replace_single("loc_add_del", loc_add, loc_del)

    return curr_text


def format_plural(unit):
    """
    Returns a formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return "s" if unit != 1 else ""


def format_age(birthday: datetime.datetime):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return "{} {}, {} {}, {} {}{}".format(
        diff.years,
        "year" + format_plural(diff.years),
        diff.months,
        "month" + format_plural(diff.months),
        diff.days,
        "day" + format_plural(diff.days),
        " 🎂" if (diff.months == 0 and diff.days == 0) else "",
    )


def simple_request(func_name, query, variables, check_status_code=True):
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if not check_status_code:
        return request

    if request.status_code == 200:
        return request
    raise Exception(func_name, " has failed with a", request.status_code, request.text)


def fetch_user(username):
    query = """
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }
    """
    variables = {"login": username}
    request = simple_request(fetch_user.__name__, query, variables)
    return {"id": request.json()["data"]["user"]["id"]}, request.json()["data"]["user"][
        "createdAt"
    ]


USER_ID, ACC_DATE = fetch_user(USER_NAME)


def fetch_followers(username):
    query = """
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }
    """
    request = simple_request(fetch_followers.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["followers"]["totalCount"])


# TODO: this only runs for the first 100 repos (only affects star count)
def fetch_repos_stars(count_type: Literal["repos", "stars"], owner_affiliation) -> int:
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!) {
        user(login: $login) {
            repositories(first: 100, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }
    """
    variables = {
        "owner_affiliation": owner_affiliation,
        "login": USER_NAME,
    }
    request = simple_request(fetch_repos_stars.__name__, query, variables)
    if count_type == "repos":
        return request.json()["data"]["user"]["repositories"]["totalCount"]
    elif count_type == "stars":
        data = request.json()["data"]["user"]["repositories"]["edges"]
        total_stars = 0
        for node in data:
            total_stars += node["node"]["stargazers"]["totalCount"]
        return total_stars


def fetch_loc(owner_affiliation, comment_size=0):
    def collect_edges(curr_edges=[], cursor=None):
        query = """
        query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
        }
        """
        variables = {
            "owner_affiliation": owner_affiliation,
            "login": USER_NAME,
            "cursor": cursor,
        }
        request = simple_request(fetch_loc.__name__, query, variables)
        curr_edges += request.json()["data"]["user"]["repositories"]["edges"]
        if request.json()["data"]["user"]["repositories"]["pageInfo"]["hasNextPage"]:
            return collect_edges(
                curr_edges,
                request.json()["data"]["user"]["repositories"]["pageInfo"]["endCursor"],
            )
        return curr_edges

    edges = collect_edges()
    return build_cache(edges, comment_size, False)


def get_cache_filename():
    return Path(f"cache/{hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()}.txt")


# checks each repository in edges to see if its been updated since last cache; runs recursive_loc if it has
def build_cache(edges, comment_size, force_cache):
    cached = True
    filename = get_cache_filename()
    try:
        with open(filename, "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append(
                    "This line is a comment block. Write whatever you want here.\n"
                )
        with open(filename, "w") as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]  # save the comment block
    data = data[comment_size:]  # remove the comment block
    for index in range(len(edges)):
        repo_hash, commit_count, *_ = data[index].split()
        if (
            repo_hash
            == hashlib.sha256(
                edges[index]["node"]["nameWithOwner"].encode("utf-8")
            ).hexdigest()
        ):
            try:
                if (
                    int(commit_count)
                    != edges[index]["node"]["defaultBranchRef"]["target"]["history"][
                        "totalCount"
                    ]
                ):
                    # if commit count has changed, update loc
                    owner, repo_name = edges[index]["node"]["nameWithOwner"].split("/")
                    print(f"cache: updating repo {repo_name}")
                    commits, additions, deletions = recursive_loc(
                        owner,
                        repo_name,
                        lambda: force_close_file(filename, data, cache_comment),
                    )
                    total_count = edges[index]["node"]["defaultBranchRef"]["target"][
                        "history"
                    ]["totalCount"]
                    data[index] = (
                        " ".join(
                            [
                                repo_hash,
                                str(total_count),
                                str(commits),
                                str(additions),
                                str(deletions),
                            ]
                        )
                        + "\n"
                    )
            except TypeError:  # if the repo is empty
                data[index] = repo_hash + " 0 0 0 0\n"

    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)

    total_add = 0
    total_del = 0
    for line in data:
        loc = line.split()
        total_add += int(loc[3])
        total_del += int(loc[4])
    return total_add, total_del, total_add - total_del, cached


# wipes the cache file
def flush_cache(edges, filename, comment_size):
    with open(filename, "r") as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]  # only save the comment
    with open(filename, "w") as f:
        f.writelines(data)
        for node in edges:
            f.write(
                hashlib.sha256(
                    node["node"]["nameWithOwner"].encode("utf-8")
                ).hexdigest()
                + " 0 0 0 0\n"
            )


# forces the cache file to close, preserving whatever data was written to it
def force_close_file(filename, data, cache_comment):
    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print(
        "There was an error while writing to the cache file. The file has had the partial data saved and closed."
    )


# fetches 100 commits from a repo at a time
def recursive_loc(owner, repo_name, force_close_file):
    def fetch_loc(cursor=None, commits=0, additions=0, deletions=0):
        query = """
        query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
        }
        """
        variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
        request = simple_request(
            recursive_loc.__name__, query, variables, check_status_code=False
        )
        if request.status_code == 200:
            if (
                request.json()["data"]["repository"]["defaultBranchRef"] != None
            ):  # only count commits if repo isn't empty
                history = request.json()["data"]["repository"]["defaultBranchRef"][
                    "target"
                ]["history"]
                for node in history["edges"]:
                    if node["node"]["author"]["user"] == USER_ID:
                        commits += 1
                        additions += node["node"]["additions"]
                        deletions += node["node"]["deletions"]
                if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
                    return commits, additions, deletions
                else:
                    return fetch_loc(
                        history["pageInfo"]["endCursor"], commits, additions, deletions
                    )
            else:
                return commits, additions, deletions
        force_close_file()  # saves what is currently in the file before the program crashes
        if request.status_code == 403:
            raise Exception(
                "Too many requests in a short amount of time!\nYou've hit the non-documented anti-abuse limit!"
            )
        raise Exception(
            f"{recursive_loc.__name__} has failed with a {request.status_code} {request.text}"
        )

    return fetch_loc()


# tallies total commits using cache_builder file
def count_commits(comment_size):
    total = 0
    filename = get_cache_filename()
    with open(filename, "r") as f:
        data = f.readlines()[comment_size:]
    for line in data:
        total += int(line.split()[2])
    return total


def main():
    age_string = format_age(
        datetime.datetime(BIRTH["YEAR"], BIRTH["MONTH"], BIRTH["DAY"])
    )
    loc_add, loc_del, loc_total, _ = fetch_loc(
        ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], COMMENT_SIZE
    )
    repos_owner = fetch_repos_stars("repos", ["OWNER"])
    stars_owner = fetch_repos_stars("stars", ["OWNER"])

    repos_total = fetch_repos_stars(
        "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )

    followers = fetch_followers(USER_NAME)

    commits = count_commits(COMMENT_SIZE)

    print(f"age - {age_string}")
    print(f"loc - add: {loc_add}; del: {loc_del}; total: {loc_total}")
    print(f"repos - owner: {repos_owner}; total: {repos_total}")
    print(f"stars - {stars_owner}")
    print(f"followers - {followers}")
    print(f"commits - {commits}")

    with open("README.md", "r", encoding="utf-8") as f:
        text = f.read()

    new_text = replace_all(
        text,
        age_string,
        repos_owner,
        repos_total,
        stars_owner,
        commits,
        followers,
        loc_total,
        loc_add,
        loc_del,
    )

    with open("README.md", "w") as f:
        f.write(new_text)

    print("\nsuccessfully updated README.md")


if __name__ == "__main__":
    main()
