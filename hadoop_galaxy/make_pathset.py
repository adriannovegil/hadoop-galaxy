import argparse
import os
import subprocess
import sys
import urlparse

import pydoop
import pydoop.hdfs as phdfs

from hadoop_galaxy.pathset import FilePathset

ValidModes = ('default', 'local')

def log(*args):
    print >> sys.stderr, ' '.join(args)

class Uri(object):
    def __init__(self, *args):
        if len(args) == 1 and all(hasattr(args[0], attr) for attr in ('scheme', 'netloc', 'path')):
            self.scheme = args[0].scheme
            self.netloc = args[0].netloc
            self.path = args[0].path
        elif len(args) == 3:
            self.scheme, self.netloc, self.path = args
        else:
            raise ValueError()
        if self.scheme == 'file':
            if self.netloc:
                raise ValueError("Can't specify a netloc with file: scheme")
            if self.path and not self.path.startswith('/'):
                raise ValueError("Must use absolute paths with file: scheme (found %s)" % self.path)
        if self.netloc and not self.scheme:
            raise ValueError("Can't specify a host without an access scheme")

    def geturl(self):
        if self.scheme:
            url = "%s://%s%s" % (self.scheme, self.netloc, self.path)
        else:
            url = self.path
        return url

def get_default_fs():
    root_ls = phdfs.ls('/')
    if root_ls:
        uri = Uri(urlparse.urlparse(root_ls[0]))
        return uri
    else:
        raise RuntimeError("Could not determine URI of default file system.  It's empty.")

def resolve_datapath(mode, datapath):
    """
    Returns a full URI for datapath
    """
    u = Uri(urlparse.urlparse(datapath))

    if not u.path:
        raise RuntimeError("blank path in %s" % datapath)

    if mode == 'default' and not u.scheme: # datapath not specified completely. Assume it's on the default fs
        u = Uri(urlparse.urlparse(phdfs.path.abspath(u.path)))
    elif mode == 'local':
        if u.scheme and u.scheme != 'file':
            raise RuntimeError("Specified local mode but datapath is a URI with scheme %s (expected no scheme or 'file')" % u.scheme)
        # force the 'file' scheme and make the path absolute
        u.scheme = 'file'
        u.netloc = ''
        u.path = os.path.abspath(datapath)
    return u

def expand_paths(datapath_uri):
    """
    If a URI contains wildcards, this function expands them.

    Returns a list of URIs.
    """
    # simple case:  the path simply exists
    if phdfs.path.exists(datapath_uri.geturl()):
        return [datapath_uri.geturl()]

    # second case:  the path doesn't exist as it is.  It may contain wildcards, so we try
    # listing the datapath with hadoop dfs.  If we were to list with
    # pydoop.hdfs.ls we'd have to implement hadoop wildcards ourselves (perhaps with fnmatch)

    def process(ls_line):
        path = ls_line[(ls_line.rindex(' ') + 1):]
        url = Uri(urlparse.urlparse(path))
        url.scheme = datapath_uri.scheme
        url.netloc = datapath_uri.netloc
        return url.geturl()

    try:
        # run -ls with hadoop dfs the process the output.
        # We drop the first line since it's something like "Found xx items".
        ls_output = subprocess.check_output([pydoop.hadoop_exec(), 'dfs', '-ls', datapath_uri.geturl()]).rstrip('\n').split('\n')[1:]
        # for each data line, run apply the 'process' function to transform it into a full URI
        return map(process, ls_output)
    except subprocess.CalledProcessError as e:
        log("Could not list datapath %s.  Please check whether it exists" % datapath_uri.geturl())
        log("Message:", str(e))
        sys.exit(1)

def parse_args(args=None):
    parser = argparse.ArgumentParser(description="Make a pathset file from one or more paths")
    parser.add_argument('--force-local', action='store_true', help="Force path to be local (i.e., URI starting with file://")
    parser.add_argument('--data-format', help="Set the type of the pathset contents to this data type (e.g. 'fastq')")
    parser.add_argument('output_path', help="Pathset file to write")
    parser.add_argument('paths', nargs='*', help="Paths to be written to the pathset. Alternatively, provide the on stdin, one per line.")
    return parser.parse_args(args)

def test_hadoop():
    """
    Test the hadoop configuration.
    Calls sys.exit if test fails.
    """
    cmd = [pydoop.hadoop_exec(), 'dfs', '-stat', 'file:///']
    try:
        subprocess.check_output(cmd)
    except subprocess.CalledProcessError as e:
        log("Error running hadoop program.  Please check your environment (tried %s)" % ' '.join(cmd))
        log("Message:", str(e))
        sys.exit(2)

def do_work(options):
    mode = 'local' if options.force_local else 'default'
    output_path = options.output_path
    data_format = options.data_format # may be None

    if options.paths:
        data_paths = options.paths
    else:
        log("reading paths from stdin")
        data_paths = [ line.rstrip() for line in sys.stdin ]
    log("read %s paths" % len(data_paths))

    # this is the real work
    uris = [ resolve_datapath(mode, p) for p in data_paths ]
    expanded_uris = [ u for wild in uris for u in expand_paths(wild) ]
    output_pathset = FilePathset(*expanded_uris)
    output_pathset.set_datatype(data_format)
    with open(output_path, 'w') as f:
        output_pathset.write(f)

def main(args=None):
    args = args or sys.argv[1:]
    options = parse_args(args)
    test_hadoop() # calls sys.exit if test fails
    do_work(options)
