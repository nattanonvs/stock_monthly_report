# -*- coding: utf-8 -*-
import os
import sys
import zipfile


def main():
    module_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    module_name = os.path.basename(module_dir)
    out_path = os.path.abspath(os.path.join(module_dir, f"{module_name}.zip"))

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(module_dir):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".idea", ".venv"}]
            for fn in files:
                if fn.endswith((".pyc",)):
                    continue
                abs_path = os.path.join(root, fn)
                rel = os.path.relpath(abs_path, os.path.dirname(module_dir))
                zf.write(abs_path, rel)

    sys.stdout.write(out_path + "\n")


if __name__ == "__main__":
    main()

