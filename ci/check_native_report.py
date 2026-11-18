"""Require executed native cases with zero failures, errors or skips."""
import argparse
import xml.etree.ElementTree as ET

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('report')
args = parser.parse_args()
cases = ET.parse(args.report).getroot().findall('.//testcase')
if not cases or any(case.find(status) is not None for case in cases for status in ('failure', 'error', 'skipped')):
    raise SystemExit('Native acceptance requires executed cases with zero failures, errors and skips')
print(f'{len(cases)} native cases executed without failures, errors or skips')
