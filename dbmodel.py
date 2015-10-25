# Definitions of objects stored in Data Store and Memcache

from google.appengine.ext import ndb

# There is one of these objects in the datastore, to persist some bits of info
# that we need across update runs. keyname is "global".
class GlobalInfo(ndb.Model):
    docdir_etag = ndb.BlobProperty()
    # HTTP ETag of the vim repository request for the 'runtime/doc' subdirectory

    master_etag = ndb.BlobProperty()
    # HTTP ETag of the commit that the master branch points to

    vim_version = ndb.BlobProperty()
    # Current Vim version

# Info related to an unprocessed documentation file from the repository; key
# name is basename, e.g. "help.txt"
class RawFileInfo(ndb.Model):
    sha1 = ndb.BlobProperty(required=True)
    # SHA1 of content

    etag = ndb.BlobProperty()
    # HTTP ETag of the file on github

# The actual contents of an unprocessed documentation file from the repository;
# key name is basename, e.g. "help.txt"
class RawFileContent(ndb.Model):
    data = ndb.BlobProperty(required=True)
    # The data

    encoding = ndb.BlobProperty(required=True)
    # The encoding, e.g. 'UTF-8'

# Info related to a processed (HTMLified) documentation file; key name is
# basename, e.g. "help.txt"
class ProcessedFileHead(ndb.Model):
    etag = ndb.BlobProperty()
    # HTTP ETag on this server, generated by us as a hash of the contents

    encoding = ndb.BlobProperty(required=True)
    # Encoding, always matches the corresponding 'RawFileContent' object

    modified = ndb.DateTimeProperty(indexed=False, auto_now=True)
    # Time when this file was generated

    numparts = ndb.IntegerProperty(indexed=False)
    # Number of parts; there will be 'numparts - 1' objects of kind
    # 'ProcessedFilePart' in the database. Processed files are split up into
    # parts as required by datastore blob limitations (currently these can only
    # be up to 1 MiB in size)

    data0 = ndb.BlobProperty(required=True)
    # Contents of the first (and possibly only) part

# Part of a processed file; keyname is basename + ":" + partnum (1-based), e.g.
# "help.txt:1".
# This chunking is necessary because the maximum entity size in the Datastore is
# 1 MB: see https://cloud.google.com/appengine/docs/python/ndb/
class ProcessedFilePart(ndb.Model):
    data = ndb.BlobProperty(required=True)
    # Contents