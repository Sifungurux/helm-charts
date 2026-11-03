#!/usr/bin/env python3
"""Resolve a Hugging Face revision to a commit SHA, and a file's sha256.

    hf_resolve.py REPO REVISION [FILE]  ->  prints "<commit-sha> [<sha256>]"

Reads only metadata (no download). HF_TOKEN in the environment is used for
gated repositories.
"""
import os
import sys

from huggingface_hub import HfApi


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    repo, revision = sys.argv[1], sys.argv[2]
    path = sys.argv[3] if len(sys.argv) == 4 else ""
    api = HfApi(token=os.environ.get("HF_TOKEN") or None)
    sha = api.model_info(repo, revision=revision).sha
    out = [sha]
    if path:
        info = api.get_paths_info(repo, [path], revision=sha)
        if not info or getattr(info[0], "lfs", None) is None:
            sys.exit(f"{repo}@{sha}: {path} not found or not an LFS file")
        out.append(info[0].lfs.sha256)
    print(" ".join(out))


if __name__ == "__main__":
    main()
