import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PY311_DIR = os.path.join(SCRIPT_DIR, "python311")

if os.path.isdir(PY311_DIR):
    paths = [PY311_DIR, os.path.join(PY311_DIR, "Scripts")]
    os.environ["PATH"] = os.pathsep.join(paths + [os.environ.get("PATH", "")])

import runpy

def run_script(script, args):
    script = os.path.abspath(script)

    if not os.path.isfile(script):
        print(f"ERROR: script not found: {script}", file=sys.stderr)
        sys.exit(1)

    sys.argv = [ script ] + args

    try:
        runpy.run_path(script, run_name = "__main__")
        err_code = 0
    except SystemExit as e:
        err_code = e.code if isinstance(e.code, int) else 0
    sys.exit(err_code)


if __name__ == "__main__":
    argv = sys.argv[1:]

    if not argv or not argv[0]:
        run_script("wf2hlp.py", [ "-i" ])

    arg1 = argv[0]
    if arg1.endswith(".py"):
        run_script(arg1, argv[1:])
    else:
        run_script("wf2hlp.py", argv)
