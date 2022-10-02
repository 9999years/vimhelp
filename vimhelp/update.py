# Regularly scheduled update: check which files need updating and process them

# Ugh, the GitHub GraphQL API does not seem to support ETag:
# https://github.com/github-community/community/discussions/10799

import base64
import hashlib
import json
import logging
import os
import re
from http import HTTPStatus

import flask
import flask.views
import gevent
import gevent.pool
import werkzeug.exceptions

import google.cloud.ndb
import google.cloud.tasks

from .dbmodel import (
    GlobalInfo,
    ProcessedFileHead,
    ProcessedFilePart,
    RawFileContent,
    RawFileInfo,
    TagsInfo,
    ndb_client,
)
from .http import HttpClient, HttpResponse
from . import secret
from . import vimh2h

# Once we have consumed about ten minutes of CPU time, Google will throw us a
# DeadlineExceededError and our script terminates. Therefore, we must be careful with
# the order of operations, to ensure that after this has happened, the next scheduled
# run of the script can pick up where the previous one was interrupted. Although in
# practice, it takes about 30 seconds, so it's unlikely to be an issue.

# Number of concurrent (in the gevent sense) workers. Avoid setting this too high, else
# there is risk of running out of memory on our puny worker node.
CONCURRENCY = 5

TAGS_NAME = "tags"
FAQ_NAME = "vim_faq.txt"
HELP_NAME = "help.txt"

DOC_ITEM_RE = re.compile(r"(?:[-\w]+\.txt|tags)$")
VERSION_TAG_RE = re.compile(r"v?(\d[\w.+-]+)$")

GITHUB_DOWNLOAD_URL_BASE = "https://raw.githubusercontent.com/vim/vim/"
GITHUB_GRAPHQL_API_URL = "https://api.github.com/graphql"

GITHUB_GRAPHQL_QUERIES = {
    "GetRefs": """
        query GetRefs {
          repository(owner: "vim", name: "vim") {
            defaultBranchRef {
              target {
                oid
              }
            }
            refs(refPrefix: "refs/tags/",
                 orderBy: {field: TAG_COMMIT_DATE, direction: DESC},
                 first: 5) {
              nodes {
                name
              }
            }
          }
        }
        """,
    "GetDir": """
        query GetDir($expr: String) {
          repository(owner: "vim", name: "vim") {
            object(expression: $expr) {
              ... on Tree {
                entries {
                  type
                  name
                  oid
                }
              }
            }
          }
        }
        """,
}

FAQ_BASE_URL = "https://raw.githubusercontent.com/chrisbra/vim_faq/master/doc/"

PFD_MAX_PART_LEN = 995000


class UpdateHandler(flask.views.MethodView):
    def post(self):
        # We get an HTTP POST request if the request came programmatically via Cloud
        # Tasks.
        self._run(flask.request.data)
        return flask.Response()

    def get(self):
        # We get an HTTP GET request if the request was generated by the user, by
        # entering the URL in their browser.
        self._run(flask.request.query_string)
        return "Success."

    def _run(self, request_data):
        req = flask.request

        # https://cloud.google.com/tasks/docs/creating-appengine-handlers#reading_app_engine_task_request_headers
        if (
            "X-AppEngine-QueueName" not in req.headers
            and os.environ.get("VIMHELP_ENV") != "dev"
            and secret.UPDATE_PASSWORD not in request_data
        ):
            raise werkzeug.exceptions.Forbidden()

        is_force = b"force" in request_data

        logging.info("Starting %supdate", "forced " if is_force else "")

        self._http_client = HttpClient(CONCURRENCY)

        try:
            self._greenlet_pool = gevent.pool.Pool(size=CONCURRENCY)

            with ndb_client.context():
                self._g = self._init_g(wipe=is_force)
                self._g_dict_pre = self._g.to_dict()
                self._had_exception = False
                self._do_update(no_rfi=is_force)

                if not self._had_exception and self._g_dict_pre != self._g.to_dict():
                    self._g.put()
                    logging.info("Finished update, updated global info")
                else:
                    logging.info("Finished update, global info not updated")

            self._greenlet_pool.join()
        finally:
            self._http_client.close()

    def _init_g(self, wipe):
        """Initialize 'self._g' (GlobalInfo)"""
        g = GlobalInfo.get_by_id("global")

        if wipe:
            logging.info("Deleting global info and raw files from Datastore")
            greenlets = [
                self._spawn(wipe_db, RawFileContent),
                self._spawn(wipe_db, RawFileInfo),
            ]
            if g:
                greenlets.append(self._spawn(g.key.delete))
                g = None
            gevent.joinall(greenlets)

        if not g:
            g = GlobalInfo(id="global")

        logging.info(
            "Global info: %s",
            ", ".join("{} = {}".format(n, getattr(g, n)) for n in g._properties.keys()),
        )

        return g

    def _do_update(self, no_rfi):

        old_vim_version = self._g.vim_version
        old_master_sha = self._g.master_sha

        # Kick off retrieval of master branch SHA and vim version from GitHub
        get_git_refs_greenlet = self._spawn(self._get_git_refs)

        # Kick off retrieval of all RawFileInfo entities from the Datastore
        if not no_rfi:
            all_rfi_greenlet = self._spawn(lambda: RawFileInfo.query().fetch())

        # Check whether the master branch is updated, and whether we have a new vim
        # version
        get_git_refs_greenlet.get()
        is_master_updated = self._g.master_sha != old_master_sha
        is_new_vim_version = self._g.vim_version != old_vim_version

        if is_master_updated:
            # Kick off retrieval of 'runtime/doc' dir listing in GitHub. This is against
            # the 'master' branch, since the docs often get updated after the tagged
            # commits that introduce the relevant changes.
            docdir_expr = self._g.master_sha + ":runtime/doc"
            docdir_greenlet = self._spawn(
                self._github_graphql_request,
                "GetDir",
                variables={"expr": docdir_expr},
                etag=self._g.docdir_etag,
            )
        else:
            docdir_greenlet = None

        # Put all RawFileInfo entites into a map
        self._rfi_map = {}
        if not no_rfi:
            self._rfi_map = {r.key.id(): r for r in all_rfi_greenlet.get()}

        # Kick off FAQ download
        faq_greenlet = self._spawn(
            self._get_file, "http", FAQ_NAME, base_url=FAQ_BASE_URL
        )

        # Iterate over 'runtime/doc' dir listing and collect list of changed files
        changed_file_names = set()

        if docdir_greenlet is not None:
            docdir = docdir_greenlet.get()

        if docdir_greenlet is None:
            logging.info("No need to get new doc dir listing")
        elif docdir.status_code == HTTPStatus.NOT_MODIFIED:
            logging.info("Doc dir not modified")
        elif docdir.status_code == HTTPStatus.OK:
            etag = docdir.header("ETag")
            self._g.docdir_etag = etag.encode() if etag is not None else None
            logging.info("Doc dir modified, new etag is %s", etag)
            resp = json.loads(docdir.body)["data"]
            for item in resp["repository"]["object"]["entries"]:
                name = item["name"]
                if item["type"] != "blob" or not DOC_ITEM_RE.match(name):
                    continue
                git_sha = item["oid"].encode()
                rfi = self._rfi_map.get(name)
                if rfi is None:
                    logging.info("Found new '%s'", name)
                elif rfi.git_sha == git_sha:
                    logging.debug("Found unchanged '%s'", name)
                    continue
                else:
                    logging.info("Found changed '%s'", name)
                    rfi.git_sha = git_sha
                changed_file_names.add(name)
        else:
            raise RuntimeError(f"Bad doc dir HTTP status {docdir.status_code}")

        # Check FAQ download result
        faq_result = faq_greenlet.get()
        if not faq_result.is_modified:
            if len(changed_file_names) == 0:
                logging.info("Nothing to do")
                return
            faq_result = None
            faq_greenlet = self._spawn(self._get_file, "db", FAQ_NAME)

        # Get tags file from GitHub or datastore, depending on whether it was changed
        if TAGS_NAME in changed_file_names:
            changed_file_names.remove(TAGS_NAME)
            tags_greenlet = self._spawn(self._get_file, "http,db", TAGS_NAME)
        else:
            tags_greenlet = self._spawn(self._get_file, "db", TAGS_NAME)

        if faq_result is None:
            faq_result = faq_greenlet.get()

        tags_result = tags_greenlet.get()

        logging.info("Beginning vimhelp-to-HTML conversions")

        # Construct the vimhelp-to-html converter, providing it the tags file content,
        # and adding on the FAQ for extra tags
        self._h2h = vimh2h.VimH2H(
            tags_result.content.decode(), version=self._g.vim_version
        )
        self._h2h.add_tags(FAQ_NAME, faq_result.content.decode())

        greenlets = []

        def track_spawn(f, *args, **kwargs):
            greenlets.append(self._spawn(f, *args, **kwargs))

        # Save tags JSON
        greenlets.append(self._spawn(self._save_tags_json))

        # Process tags file if it was modified
        if tags_result.is_modified:
            track_spawn(self._process, TAGS_NAME, tags_result.content)

        # Process FAQ if it was modified, or if tags file was modified (because it could
        # lead to a different set of links in the FAQ)
        if faq_result.is_modified or tags_result.is_modified:
            track_spawn(self._process, FAQ_NAME, faq_result.content)

        # If we found a new vim version, ensure we process help.txt, since we're
        # displaying the current vim version in the rendered help.txt.html
        if is_new_vim_version:
            track_spawn(
                self._get_file_and_process, HELP_NAME, process_if_not_modified=True
            )
            changed_file_names.discard(HELP_NAME)

        # Process all other modified files, after retrieving them from GitHub or
        # datastore
        # TODO: theoretically we should re-process all files (whether in
        # changed_file_names or not) if the tags file was modified
        for name in changed_file_names:
            track_spawn(self._get_file_and_process, name, process_if_not_modified=False)

        logging.info("Waiting for everything to finish")

        # We can't just iterate over the greenlets in the pool directly
        # ("for greenlet in self._greenlet_pool") because that set can drop elements (as
        # greenlets finish) while we're iterating over it; hence the need for our local
        # 'greenlets' list.
        for greenlet in greenlets:
            try:
                greenlet.get()
            except Exception as e:
                logging.error(e)
                self._had_exception = True

        self._greenlet_pool.join()

        logging.info("All done")

    def _get_git_refs(self):
        """
        Populate 'master_sha', 'vim_version, 'refs_etag' members of 'self._g'
        (GlobalInfo)
        """
        r = self._github_graphql_request("GetRefs", etag=self._g.refs_etag)
        if r.status_code == HTTPStatus.OK:
            etag_str = r.header("ETag")
            etag = etag_str.encode() if etag_str is not None else None
            if etag == self._g.refs_etag:
                logging.info("GetRefs query ETag unchanged (%s)", etag)
            else:
                logging.info(
                    "GetRefs query ETag changed: %s -> %s", self._g.refs_etag, etag
                )
                self._g.refs_etag = etag
            resp = json.loads(r.body)["data"]["repository"]
            latest_sha = resp["defaultBranchRef"]["target"]["oid"]
            if latest_sha == self._g.master_sha:
                logging.info("master SHA unchanged (%s)", latest_sha)
            else:
                logging.info(
                    "master SHA changed: %s -> %s", self._g.master_sha, latest_sha
                )
                self._g.master_sha = latest_sha
            tags = resp["refs"]["nodes"]
            latest_version = None
            for tag in tags:
                if m := VERSION_TAG_RE.match(tag["name"]):
                    latest_version = m.group(1)
                    break
            if latest_version == self._g.vim_version:
                logging.info("Vim version unchanged (%s)", latest_version)
            else:
                logging.info(
                    "Vim version changed: %s -> %s", self._g.vim_version, latest_version
                )
                self._g.vim_version = latest_version
        elif r.status_code == HTTPStatus.NOT_MODIFIED and self._g.refs_etag:
            logging.info("Initial GraphQL request: HTTP Not Modified")
        else:
            raise RuntimeError(
                f"Initial GraphQL request: bad HTTP status {r.status_code}"
            )

    def _github_graphql_request(self, query_name, variables=None, etag=None):
        logging.info("Making GitHub GraphQL query: %s", query_name)
        headers = {
            "Authorization": "token " + secret.GITHUB_ACCESS_TOKEN,
        }
        if etag is not None:
            headers["If-None-Match"] = etag.decode()
        body = {"query": GITHUB_GRAPHQL_QUERIES[query_name]}
        if variables is not None:
            body["variables"] = variables
        response = self._http_client.post(
            GITHUB_GRAPHQL_API_URL, json=body, headers=headers
        )
        logging.info("GitHub %s HTTP status: %s", query_name, response.status_code)
        return response

    def _save_tags_json(self):
        tags = self._h2h.sorted_tag_href_pairs()
        logging.info("Saving %d tag, href pairs", len(tags))
        TagsInfo(id="tags", tags=tags).put()

    def _get_file_and_process(self, name, process_if_not_modified):
        sources = "http,db" if process_if_not_modified else "http"
        result = self._get_file(sources, name)
        if process_if_not_modified or result.is_modified:
            self._process(name, result.content)

    def _get_file(self, sources, name, base_url=None):
        """
        Get file via HTTP and/or from the Datastore, based on 'sources', which should
        be one of "http", "db", "http,db"
        """
        sources_set = set(sources.split(","))
        rfi = self._rfi_map.get(name)
        if rfi is None:
            rfi = self._rfi_map[name] = RawFileInfo(id=name)
        result = None

        if "http" in sources_set:
            if base_url is None:
                base_url = (
                    f"{GITHUB_DOWNLOAD_URL_BASE}{self._g.master_sha}/runtime/doc/"
                )
            url = base_url + name
            headers = {}
            if rfi.etag is not None:
                headers["If-None-Match"] = rfi.etag.decode()
            logging.info("Fetching %s", url)
            response = self._http_client.get(url, headers)
            logging.info("Fetched %s -> HTTP %s", url, response.status_code)
            result = GetFileResult(response)  # raises exception on bad HTTP status
            if (etag := response.header("ETag")) is not None:
                rfi.etag = etag.encode()
            if result.is_modified:
                save_raw_file(rfi, result.content)
                return result

        if "db" in sources_set:
            logging.info("Fetching %s from datastore", name)
            rfc = RawFileContent.get_by_id(name)
            logging.info("Fetched %s from datastore", name)
            return GetFileResult(rfc)

        return result

    def _process(self, name, content):
        logging.info("Translating '%s' to HTML", name)
        phead, pparts = to_html(name, content, self._h2h)
        logging.info("Saving HTML translation of '%s' to Datastore", name)
        save_transactional([phead] + pparts)

    def _spawn(self, f, *args, **kwargs):
        def g():
            with ndb_client.context():
                return f(*args, **kwargs)

        return self._greenlet_pool.spawn(g)


class GetFileResult:
    def __init__(self, obj):
        if isinstance(obj, HttpResponse):
            self.content = obj.body
            if obj.status_code == HTTPStatus.OK:
                self.is_modified = True
            elif obj.status_code == HTTPStatus.NOT_MODIFIED:
                self.is_modified = False
            else:
                raise RuntimeError(
                    f"Fetching {obj.url} yielded bad HTTP status {obj.status_code}"
                )
        elif isinstance(obj, RawFileContent):
            self.content = obj.data
            self.is_modified = False


def to_html(name, content, h2h):
    content_str = content.decode()
    html = h2h.to_html(name, content_str).encode()
    etag = base64.b64encode(sha1(html))
    datalen = len(html)
    phead = ProcessedFileHead(id=name, encoding=b"UTF-8", etag=etag)
    pparts = []
    if datalen > PFD_MAX_PART_LEN:
        phead.numparts = 0
        for i in range(0, datalen, PFD_MAX_PART_LEN):
            part = html[i : (i + PFD_MAX_PART_LEN)]
            if i == 0:
                phead.data0 = part
            else:
                partname = f"{name}:{phead.numparts}"
                pparts.append(ProcessedFilePart(id=partname, data=part, etag=etag))
            phead.numparts += 1
    else:
        phead.numparts = 1
        phead.data0 = html
    return phead, pparts


def save_raw_file(rfi, content):
    name = rfi.key.id()
    if name in (HELP_NAME, FAQ_NAME, TAGS_NAME):
        logging.info("Saving raw file '%s' (info and content) to Datastore", name)
        rfc = RawFileContent(id=name, data=content, encoding=b"UTF-8")
        save_transactional([rfi, rfc])
    else:
        logging.info("Saving raw file '%s' (info only) to Datastore", name)
        rfi.put()


def wipe_db(model):
    all_keys = model.query().fetch(keys_only=True)
    google.cloud.ndb.delete_multi(all_keys)


@google.cloud.ndb.transactional(xg=True)
def save_transactional(entities):
    google.cloud.ndb.put_multi(entities)


def sha1(content):
    digest = hashlib.sha1()
    digest.update(content)
    return digest.digest()


def handle_enqueue_update():
    req = flask.request

    is_cron = req.headers.get("X-AppEngine-Cron") == "true"

    # https://cloud.google.com/appengine/docs/standard/python3/scheduling-jobs-with-cron-yaml?hl=en_GB#validating_cron_requests
    if (
        not is_cron
        and os.environ.get("VIMHELP_ENV") != "dev"
        and secret.UPDATE_PASSWORD not in req.query_string
    ):
        raise werkzeug.exceptions.Forbidden()

    logging.info("Enqueueing update")

    client = google.cloud.tasks.CloudTasksClient()
    queue_name = client.queue_path(
        os.environ["GOOGLE_CLOUD_PROJECT"], "us-central1", "update2"
    )
    task = {
        "app_engine_http_request": {
            "http_method": "POST",
            "relative_uri": "/update",
            "body": req.query_string,
        }
    }
    response = client.create_task(parent=queue_name, task=task)
    logging.info("Task %s enqueued, ETA %s", response.name, response.schedule_time)

    if is_cron:
        return flask.Response()
    else:
        return "Successfully enqueued update task."
