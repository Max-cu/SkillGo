"""Offline contract checks; all fixtures are synthetic, never private reports."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from prepare_materials import prepare
from render_deck_plan import render_page, PlanError, validate_body
from cleaners import pack_skill


@pytest.fixture
def project(tmp_path):
    sources = tmp_path / 'assets/sources'
    sources.mkdir(parents=True)
    (sources / '测试素材.md').write_text('Synthetic layout fixture, no business claims.', encoding='utf8')
    plan = json.loads((ROOT / 'tests/fixtures/layout-plan.json').read_text(encoding='utf8'))
    return tmp_path, plan


def render(root, plan):
    path = root / 'assets/deck_plan.json'
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf8')
    result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/render_deck_plan.py'),
                             str(path), '--root', str(root)], capture_output=True, encoding='utf8')
    return result.returncode, json.loads(result.stdout)


def test_all_compiled_layouts_and_idempotence(project):
    root, plan = project
    code, report = render(root, plan)
    assert code == 0, report
    assert report['page_count'] == 15 and report['content_pages'] == 8
    paths = list((root / 'assets/svg_body').glob('*.svg'))
    assert len(paths) == 8
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    code, repeated = render(root, plan)
    assert code == 0 and repeated['changed_files'] == []
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}


def test_overflow_does_not_publish_partial_deck(project):
    root, plan = project
    assert render(root, plan)[0] == 0
    before = {p: p.read_bytes() for p in (root / 'assets').rglob('*') if p.is_file() and p.name != 'deck_plan.json'}
    broken = copy.deepcopy(plan)
    broken['chapters'][0]['pages'][0]['items'][0]['body'] = '已有材料。' * 300
    code, report = render(root, broken)
    assert code == 1 and report['errors'][0]['field'].endswith('.body')
    assert all(p.read_bytes() == content for p, content in before.items())


def test_missing_source_and_wrong_paragraph_type_report_exact_field(project):
    root, plan = project
    plan['chapters'][0]['pages'][0]['source_refs'] = ['assets/sources/missing.md']
    assert render(root, plan)[1]['errors'][0]['field'].endswith('.source_refs')
    plan['chapters'][0]['pages'][0]['source_refs'] = ['assets/sources/测试素材.md']
    plan['chapters'][0]['pages'][0]['items'][0]['body'] = [False]
    assert render(root, plan)[1]['errors'][0]['field'].endswith('.body')


def test_image_and_custom_pages_keep_original_svg_contract(project):
    from PIL import Image
    root, plan = project
    page = plan['chapters'][0]['pages'][0]
    image = root / 'assets/sources/image.png'
    Image.new('RGB', (640, 600), '#003C96').save(image)
    body, boxes, heights, family = render_page({**page, 'layout':'image_text',
                      'items':page['items'][:2], 'image':'assets/sources/image.png'}, root, 'page')
    target = root / 'assets/svg_body/page.svg'
    target.parent.mkdir()
    target.write_bytes(body)
    assert validate_body(target, root)[0] == [] and family == '5.1'
    custom = root / 'assets/custom_body/page.svg'
    custom.parent.mkdir()
    custom.write_bytes(body)
    result = render_page({'layout':'custom','svg':'assets/custom_body/page.svg',
                         'boxes':[list(b) for b in boxes],'content_heights':heights,'layout_family':family}, root, 'page')
    assert result[1:] == ([list(b) for b in boxes], heights, family)
    custom.write_text('<g><script>bad()</script></g>', encoding='utf8')
    with pytest.raises(PlanError, match='scripts'):
        render_page({'layout':'custom','svg':'assets/custom_body/page.svg'}, root, 'page')


def test_immutable_input_handles_old_mtime_and_docx_zip_container(tmp_path):
    source = tmp_path / 'input/report.docx'
    source.parent.mkdir()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', '<document/>')
    os.utime(source, (1, 1))
    target = tmp_path / 'assets/materials.zip'
    prepare(source, target)
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ['report.docx']
        assert archive.read('report.docx') == source.read_bytes()
    original = tmp_path / 'original.zip'
    original.write_bytes(target.read_bytes())
    prepare(original, tmp_path / 'copy.zip')
    assert (tmp_path / 'copy.zip').read_bytes() == original.read_bytes()
    with pytest.raises(ValueError):
        prepare(source.parent, source.parent / 'materials.zip')


def test_package_preserves_static_icons_and_excludes_task_material():
    names = {name.removeprefix(ROOT.name + '/') for _, name in pack_skill.collect()}
    assert sum(name.startswith('assets/icons/') and name.endswith('.svg') for name in names) >= 8000
    assert 'scripts/render_deck_plan.py' in names
    assert 'references/structured_pages.md' in names
    assert not any(name.startswith(('assets/sources/', 'assets/svg_body/', 'assets/custom_body/', 'exports/', 'icons/')) for name in names)
    assert not names.intersection({'assets/deck_plan.json', 'assets/materials.zip', 'assets/pipeline_config.json',
                                   'assets/design_spec.md', 'assets/render_manifest.json'})
