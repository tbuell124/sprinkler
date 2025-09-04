import os
import sys

# Ensure the project root is on sys.path so that the "sprinkler" module can
# be imported reliably when tests are executed from within the tests folder.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

