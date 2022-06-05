#!/usr/bin/env bash

echo "Cloning https://github.com/vim/vim.git"
tmp=$(mktemp -d)
pushd "$tmp" >/dev/null || exit
git clone --depth 1 --sparse https://github.com/vim/vim.git
cd vim || exit
git sparse-checkout set runtime/doc
popd >/dev/null || exit

vim_dir="$tmp/vim/runtime/doc"

echo "Generating helptags with Vim"
vim "+helptags $vim_dir" "+helptags $INPUT_DOC_DIRECTORY" +q

echo "Generating HTML documentation"
python3 "$(dirname "$0")/scripts/h2h.py" "$INPUT_DOC_DIRECTORY" "$vim_dir" "$INPUT_OUTPUT_DIRECTORY"
