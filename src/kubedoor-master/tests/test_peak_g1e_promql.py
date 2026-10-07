"""Evaluate the production G1 Eden percentage expression with Prometheus's query engine.

Set PROMTOOL_PATH to a promtool executable, or put promtool on PATH.
Fixtures contain observations and expectations; the expression is loaded from promql.py.
"""

import copy
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml


MASTER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MASTER_DIR))

from promql import query_dict  # noqa: E402


PROMTOOL = shutil.which(os.environ.get('PROMTOOL_PATH') or 'promtool')
FIXTURE = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parent / 'fixtures' / 'g1e_usage_percent.yaml').read_text(encoding='utf-8')
)


@pytest.mark.skipif(PROMTOOL is None, reason='promtool unavailable; set PROMTOOL_PATH or put promtool on PATH')
@pytest.mark.parametrize('case', FIXTURE['tests'], ids=lambda case: case['name'])
def test_peak_g1_eden_percentage_in_prometheus_engine(tmp_path, case):
    expression = (
        query_dict['g1e_usage_percent']
        .replace('{env}', 'ienv="prod",')
        .replace('{env_key}', 'ienv,')
        .replace('{duration}', '2m')
    )
    current_case = copy.deepcopy(case)
    for assertion in current_case['promql_expr_test']:
        assertion['expr'] = expression
    definition = {
        'evaluation_interval': FIXTURE['evaluation_interval'],
        'fuzzy_compare': FIXTURE['fuzzy_compare'],
        'tests': [current_case],
    }
    test_file = tmp_path / 'g1e-usage-percent.test.yaml'
    test_file.write_text(yaml.safe_dump(definition, sort_keys=False), encoding='utf-8')

    result = subprocess.run(
        [PROMTOOL, 'test', 'rules', str(test_file)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
