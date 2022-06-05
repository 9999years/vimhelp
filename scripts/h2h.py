#!/usr/bin/env python3

import sys
import os
import os.path
import argparse

# This adds ../ to the path.
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from vimhelp.vimh2h import VimH2H  # noqa: E402


def slurp(filename):
    f = open(filename, 'rb')
    c = f.read()
    f.close()
    return c


def usage():
    return "usage: " + sys.argv[0] + " IN_DIR OUT_DIR [BASENAMES...]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('IN_DIR', help="Input directory containing .txt files to render as Vim documentation")
    parser.add_argument('VIM_DIR', help="Input directory containing .txt files from the Vim source tree, to be used for additional tags when rendering files from IN_DIR")
    parser.add_argument('OUT_DIR', help="Directory to render .html documentation into")
    args = parser.parse_args()

    in_dir: str = args.IN_DIR
    out_dir: str = args.OUT_DIR
    vim_dir: str = args.VIM_DIR

    basenames = os.listdir(in_dir)

    print("Processing tags...")
    h2h = VimH2H(slurp(os.path.join(in_dir, 'tags')).decode(),
                 slurp(os.path.join(vim_dir, 'tags')).decode(),
                 is_web_version=False)

    for basename in basenames:
        if os.path.splitext(basename)[1] != '.txt' and basename != 'tags':
            print("Ignoring " + basename)
            continue
        print("Processing " + basename + "...")
        path = os.path.join(in_dir, basename)
        content = slurp(path)
        try:
            encoding = 'UTF-8'
            content_str = content.decode(encoding)
        except UnicodeError:
            encoding = 'ISO-8859-1'
            content_str = content.decode(encoding)
        outpath = os.path.join(out_dir, basename + '.html')
        of = open(outpath, 'wb')
        of.write(h2h.to_html(basename, content_str, encoding).encode())
        of.close()


main()
