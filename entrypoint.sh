#!/usr/bin/env bash

tmp=$(mktemp -d)
pushd "$tmp"
git clone --depth 1 --sparse https://github.com/vim/vim.git
cd vim
git sparse-checkout set runtime/doc
popd

vim_dir="$tmp/vim/runtime/doc"

vim "+helptags $vim_dir" "+helptags $INPUT_DOC_DIRECTORY" +q

python3 ./scripts/h2h.py "$INPUT_DOC_DIRECTORY" "$vim_dir" "$INPUT_OUTPUT_DIRECTORY"
