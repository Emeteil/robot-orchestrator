import sys

code = int(sys.argv[1]) if len(sys.argv) > 1 else 1
print(f"exiting with {code}", flush=True)
sys.exit(code)
