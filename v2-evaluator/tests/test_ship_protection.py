import importlib.util
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def test_memory_default_reserves_headroom_and_preserves_operator_value(tmp_path):
    spec = importlib.util.spec_from_file_location('configure_memory', REPO / 'configure-memory.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    info = tmp_path / 'meminfo'; info.write_text('MemTotal:       67108864 kB\n')
    env = tmp_path / '.env'; env.write_text('TOKEN=preserve\nCF_V2_EVALUATOR_MEMORY_LIMIT=\n')
    module.configure(env, info)
    assert env.read_text() == f'TOKEN=preserve\nCF_V2_EVALUATOR_MEMORY_LIMIT={60 << 30}\n'
    env.write_text('TOKEN=preserve\nCF_V2_EVALUATOR_MEMORY_LIMIT=32g\n')
    module.configure(env, info)
    assert env.read_text().endswith('CF_V2_EVALUATOR_MEMORY_LIMIT=32g\n')


def test_compose_caps_evaluator_without_swap_and_keeps_gateway_separate():
    services = yaml.safe_load((REPO / 'docker-compose.yml').read_text())['services']
    evaluator, gateway = services['v2-evaluator'], services['eda-gateway']
    assert evaluator['mem_limit'] == evaluator['memswap_limit']
    assert 'CF_V2_EVALUATOR_MEMORY_LIMIT' in evaluator['mem_limit']
    assert gateway['oom_score_adj'] == -900
    assert 'pid' not in gateway
