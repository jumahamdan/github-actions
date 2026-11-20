#!/usr/bin/env python3
"""Run the actual unittest suite and preserve per-case JUnit evidence."""

import io
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def startTest(self, test):
        self.cases.append(test.id())
        super().startTest(test)


result = unittest.TextTestRunner(verbosity=1, resultclass=Result).run(unittest.defaultTestLoader.discover(str(ROOT / "tests")))
suite = ET.Element("testsuite", tests=str(result.testsRun), failures=str(len(result.failures)), errors=str(len(result.errors)), skipped=str(len(result.skipped)))
failures = {test.id(): text for test, text in result.failures}
errors = {test.id(): text for test, text in result.errors}
skipped = {test.id(): text for test, text in result.skipped}
for identifier in result.cases:
    case = ET.SubElement(suite, "testcase", name=identifier)
    for kind, details in (("failure", failures), ("error", errors), ("skipped", skipped)):
        if identifier in details:
            ET.SubElement(case, kind).text = details[identifier]
target = ROOT / ".ci-output/tests.xml"
target.parent.mkdir(parents=True, exist_ok=True)
ET.ElementTree(suite).write(target, encoding="utf-8", xml_declaration=True)
raise SystemExit(0 if result.wasSuccessful() and result.testsRun else 1)
