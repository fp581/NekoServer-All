"""unittest with a time limit on each test, for the browser tests (run.sh).

A browser test that hangs (a page that never answers, a server that never stops) would
otherwise hold CI's runner until the job's own limit. Here each test has LIMIT seconds:
past that, the stack of every thread is printed (where it hangs), then the test's name,
and the run ends with exit code 3.
Usage (from nekoserver/tests): python3 -B timed.py <test module>...
"""

import faulthandler
import os
import sys
import threading
import time
import unittest

# The longest browser test takes well under a minute on CI. NEKO_TEST_LIMIT: another limit.
LIMIT = int(os.environ.get('NEKO_TEST_LIMIT', '180'))
OVER_TIME = 3
running = {'test': None, 'since': 0.0}


class Timed(unittest.TextTestResult):
    def startTest(self, test):
        running['test'], running['since'] = test, time.monotonic()
        faulthandler.dump_traceback_later(LIMIT)
        super().startTest(test)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        running['test'] = None
        super().stopTest(test)


def watch():
    while True:
        time.sleep(1)
        test = running['test']
        if test is not None and time.monotonic() - running['since'] > LIMIT + 2:
            print(f'\n::error title=Browser tests::{test.id()} ran over {LIMIT} s (its stack is above)', flush=True)
            os._exit(OVER_TIME)


def main(names):
    sys.path.insert(0, os.getcwd())
    threading.Thread(target=watch, daemon=True).start()
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Timed).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
