"""Structural chunks and bounded static call dependencies remain source evidence."""
import json

import pytest

from local_coder.memory.code_structure import structure_chunks
from local_coder.memory.context_store import ContextMemory


def test_nested_python_definitions_have_owned_spans_and_stable_anchors():
    source = 'def outer(value):\n    def inner(item):\n        return item + 1\n    return inner(value)\n\nclass Service:\n    def method(self):\n        return outer(2)\n'
    chunks = structure_chunks('pkg/code.py', source)
    assert {'outer', 'outer.inner', 'Service', 'Service.method'} <= {row['metadata']['qualified_name'] for row in chunks}
    covered = []
    for row in chunks:
        metadata = row['metadata']
        assert metadata['syntax_provider'] == 'python_ast'
        assert metadata['anchor'].startswith('code:pkg/code.py::')
        covered.extend(range(metadata['start_line'], metadata['end_line'] + 1))
    assert len(covered) == len(set(covered)), 'Nested bodies must not also be embedded inside parent chunks'
    moved = structure_chunks('pkg/code.py', '\n\n' + source)
    assert {row['metadata']['anchor'] for row in chunks} == {row['metadata']['anchor'] for row in moved}
    outer_call = next(call for row in chunks if row['metadata']['qualified_name'] == 'outer' for call in row['metadata']['calls'])
    assert outer_call['expression'] == 'inner'
    assert outer_call['resolution'] == 'static_candidate'


def test_cross_file_relative_import_helpers_are_automatically_retrieved(tmp_path):
    package = tmp_path / 'pkg'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'entry.py').write_text('from .helpers import normalize as clean\n\ndef parse_customer_record(value):\n    return clean(value)\n')
    (package / 'helpers.py').write_text('def normalize(value):\n    return value.strip()\n')
    memory = ContextMemory(tmp_path)
    memory.index_code()
    hits = memory.search('parse_customer_record', char_budget=1200)
    helper = next(hit for hit in hits if hit['metadata'].get('qualified_name') == 'normalize')
    assert helper['source_ref'] == 'pkg/helpers.py:1'
    assert helper['dependency_of']
    assert any('static call' in reason for reason in helper['reasons'])
    assert helper['metadata']['anchor']
    assert len({hit['id'] for hit in hits}) == len(hits)
    assert sum(len(hit['content']) for hit in hits) <= 1200
    context = memory.context('parse_customer_record', char_budget=500)
    assert len(context['text']) <= 500


def test_changed_deleted_and_corrected_helpers_never_reappear(tmp_path):
    entry = tmp_path / 'entry.py'
    helper = tmp_path / 'helper.py'
    entry.write_text('from helper import sanitize\n\ndef ingest_customer(value):\n    return sanitize(value)\n')
    helper.write_text('def sanitize(value):\n    return value.strip()\n')
    memory = ContextMemory(tmp_path)
    memory.index_code()
    original = next(hit for hit in memory.search('ingest_customer') if hit['source_path'] == 'helper.py')
    helper.write_text('def replacement(value):\n    return value\n')
    assert all(hit['source_path'] != 'helper.py' for hit in memory.search('ingest_customer'))
    memory.index_code(['helper.py'])
    assert all(hit['source_path'] != 'helper.py' for hit in memory.search('ingest_customer'))
    assert memory.get(original['id']) is None
    helper.write_text('def sanitize(value):\n    return value.strip()\n')
    memory.index_code(['helper.py'])
    restored = next(hit for hit in memory.search('ingest_customer') if hit['source_path'] == 'helper.py')
    memory.update(restored['id'], 'Corrected evidence; do not treat this as an AST definition')
    assert all(not hit.get('dependency_of') for hit in memory.search('ingest_customer') if hit['source_path'] == 'helper.py')
    memory.delete(restored['id'])
    assert memory.search(restored['metadata']['anchor']) == []
    helper.unlink()
    assert all(hit['source_path'] != 'helper.py' for hit in memory.search('ingest_customer'))


def test_shadowed_and_dynamic_calls_stay_unresolved(tmp_path):
    source = 'from helper import sanitize\n\ndef process_record(sanitize, value):\n    return sanitize(value)\n\ndef dynamic_record(obj, value):\n    return obj.sanitize(value)\n'
    (tmp_path / 'entry.py').write_text(source)
    (tmp_path / 'helper.py').write_text('def sanitize(value):\n    return value\n')
    memory = ContextMemory(tmp_path)
    memory.index_code()
    for query in ('process_record', 'dynamic_record'):
        hits = memory.search(query)
        assert not any(hit.get('dependency_of') for hit in hits)
        call = next(call for hit in hits for call in hit['metadata'].get('calls', []))
        assert call['resolution'] == 'unresolved'
        assert call['reason']


def test_dependency_cycles_and_fanout_are_bounded_and_not_duplicate(tmp_path):
    (tmp_path / 'cyclic.py').write_text('def entry_cycle(value):\n    return helper_cycle(value)\n\ndef helper_cycle(value):\n    return entry_cycle(value)\n')
    memory = ContextMemory(tmp_path)
    memory.index_code()
    hits = memory.search('entry_cycle', limit=8, char_budget=1000)
    assert len(hits) == 2
    assert len({hit['id'] for hit in hits}) == 2
    for budget in (0, 1, 50, 150):
        assert sum(len(hit['content']) for hit in memory.search('entry_cycle', char_budget=budget)) <= budget


def test_other_languages_and_invalid_python_have_explicit_fallbacks():
    for path, source in [('sample.go', 'func ParseThing(value string) string { return value }\n'), ('invalid.py', 'def broken(:\n    return other()\n')]:
        chunks = structure_chunks(path, source)
        assert chunks
        assert all(row['metadata']['syntax_provider'] == 'bounded_text' for row in chunks)
        assert all(row['metadata']['calls'] == [] for row in chunks)
        assert all(row['metadata']['structure_status'] == 'unsupported_language' if path.endswith('.go') else row['metadata']['structure_status'] == 'parse_failed' for row in chunks)


def test_structure_tree_and_anchor_search_are_scoped_and_bounded(tmp_path):
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / 'api.py').write_text('def stable_customer_anchor(value):\n    return value\n')
    (tmp_path / 'outside.py').write_text('def unrelated_anchor():\n    return None\n')
    memory = ContextMemory(tmp_path)
    memory.index_code()
    tree = memory.structure(['pkg'], query='customer', char_budget=600)
    assert len(tree['text']) <= 600
    assert 'pkg/api.py' in tree['text']
    assert 'outside.py' not in tree['text']
    assert tree['items'][0]['anchor']
    anchor = tree['items'][0]['anchor']
    assert memory.search(anchor)[0]['metadata']['anchor'] == anchor
    for budget in (0, 1, 60, 300):
        assert len(memory.structure(char_budget=budget)['text']) <= budget
    with pytest.raises(ValueError):
        memory.structure(['../outside'])


def test_structural_metadata_is_secret_filtered_and_never_edits_code(tmp_path):
    secret = 'sk-' + 'a' * 40
    path = tmp_path / 'source.py'
    source = f'def process_private(value):\n    api_key = "{secret}"\n    return value\n'
    path.write_text(source)
    memory = ContextMemory(tmp_path)
    memory.index_code()
    assert path.read_text() == source
    assert secret not in json.dumps(memory.list_memories())
    assert secret not in memory.structure()['text']


def test_imported_module_alias_and_src_package_helpers(tmp_path):
    package = tmp_path / 'src' / 'pkg'
    package.mkdir(parents=True)
    (package / 'entry.py').write_text('import pkg.helpers as util\n\ndef customer_entry(value):\n    return util.normalize(value)\n')
    (package / 'helpers.py').write_text('def normalize(value):\n    return value\n')
    memory = ContextMemory(tmp_path)
    memory.index_code()
    assert any(hit.get('dependency_of') and hit['source_path'] == 'src/pkg/helpers.py' for hit in memory.search('customer_entry'))


def test_large_statement_chunks_are_bounded_and_labeled():
    chunks = structure_chunks('large.py', 'def huge():\n    return "' + 'x' * 8000 + '"\n', max_chars=300)
    assert chunks and all(len(chunk['content']) <= 300 for chunk in chunks)
    assert any(chunk['metadata']['chunking'] == 'bounded_oversize' for chunk in chunks)


def test_exception_alias_does_not_resolve_to_shadowed_import():
    source = 'from helpers import helper\n\ndef parse_record():\n    try:\n        pass\n    except Exception as helper:\n        helper()\n'
    calls = [call for chunk in structure_chunks('entry.py', source) for call in chunk['metadata']['calls'] if call['expression'] == 'helper']
    assert calls and calls[0]['resolution'] == 'unresolved'
