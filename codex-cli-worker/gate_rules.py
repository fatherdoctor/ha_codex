"""The rules of the remote-control command check, shared by the Codex hook and the worker's config watch.

Commands from the ChatGPT app reach Codex through OpenAI's relay and skip this
app's Allow and Deny buttons. The check below holds outside the model:

- ``check_tool_call`` is what the managed Codex hook (``gate/gate_hook.py``)
  runs for every tool call and every permission request. It refuses named
  security restrictions and otherwise says nothing, preserving Codex's
  normal approval flow. It always refuses reads of process environments, the Supervisor token,
  Home Assistant's sign-in store and ``secrets.yaml``, and file edits that add
  a command that can reach anything or a templated action.
- ``find_refused`` is the scan both sides use on YAML text. The worker runs it
  over ``/config`` to catch edits the hook cannot see (a shell command that
  writes a file) and raises a Home Assistant repair for them.
- ``refused_blocks`` always hashes definition, platform and template blocks,
  while unchanged calls and templated targets follow normal approval.

The refused domains live in one file, ``policy/refused-domains.json``
(``/usr/share/ha_codex/refused-domains.json`` in the image). When that file is
missing the built-in list below is used; it is never looser than the file.

A bounded safe-node second pass reads flow/quoted YAML and include dependencies;
the original line scan also works on half an edit. Computed shell references
still need the native sandbox, managed policy and configuration watch.

"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import os
import shlex
import stat
import time
import posixpath
from functools import wraps
from contextvars import ContextVar
import yaml
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMAGE_DIR = Path("/usr/share/ha_codex")
SOURCE_DIR = Path(__file__).resolve().parent / "policy"
CONFIG_ROOT = Path("/config")


STRICT_RULES: dict[str, Any] = {
    "reaches_anything": ["hassio", "pyscript", "python_script", "rest_command", "shell_command"],
    "runs_on_load_platforms": ["command_line", "rest", "scrape"],
    "runs_on_load_keys": ["command_line", "rest", "rest_command", "scrape", "shell_command", "python_script", "pyscript"],
}
LIST_KEYS = ("reaches_anything", "runs_on_load_platforms", "runs_on_load_keys")


ACTION_RE = re.compile(r"""^\s*-?\s*(?:service|action)\s*:\s*["']?([a-z0-9_]+)\.([a-z0-9_]+)""", re.IGNORECASE)
TEMPLATE_ACTION_RE = re.compile(r"""^\s*-?\s*(?:(?:service|action)\s*:.*(?:\{\{|\{%)|service_template\s*:)""")
PLATFORM_RE = re.compile(r"""^\s*-?\s*platform\s*:\s*["']?([a-z0-9_]+)["']?\s*(?:#.*)?$""", re.IGNORECASE)
# A definition key can hold a block, include or inline value: all run on load.
DEFINITION_KEY_RE = re.compile(r"""^\s*-?\s*([a-z0-9_]+)\s*:""")
SCRIPT_REF_RE = re.compile(r"""\bscript\.([a-z0-9_]+)\b""", re.IGNORECASE)
SCRIPT_TARGET_RE = re.compile(r"""^\s*-?\s*(?:["']?script\.|entity_id\s*:|target\s*:\s*\{\s*entity_id\s*:)""")
# The script domain's own services are not scripts.
SCRIPT_SERVICES = {"turn_on", "turn_off", "toggle", "reload"}

# Reads refused from every client: process environments (the worker's holds the Supervisor token),
# the Supervisor itself, Home Assistant's sign-in store (its signing key) and secrets.yaml.
PROTECTED_COMMAND_RES = (
    (re.compile(r"/proc/\S*environ"), "reads a process environment, which can hold the Supervisor token"),
    (re.compile(r"\b(?:SUPERVISOR_TOKEN|HASSIO_TOKEN)\b"), "uses the Supervisor token"),
    (re.compile(r"/run/s6/container_environment"), "reads the app's saved environment, which holds the Supervisor token"),
    (re.compile(r"https?://supervisor\b|\bsupervisor/|\bha\s+(?:supervisor|su)\b|\bbashio::", re.IGNORECASE),
     "talks to the Home Assistant Supervisor"),
    (re.compile(r"\.storage/+auth"), "reads Home Assistant's sign-in store"),
    (re.compile(r"secrets\.ya?ml"), "reads secrets.yaml"),
)
PROTECTED_PATH_RES = (
    (re.compile(r"(?:^|/)\.storage/+auth"), "edits Home Assistant's sign-in store"),
    (re.compile(r"(?:^|/)secrets\.ya?ml$"), "edits secrets.yaml"),
)
PATCH_BEGIN = "*** Begin Patch"
PATCH_FILE_RE = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+?)\s*$")
YAML_SUFFIXES = (".yaml", ".yml")
PATCH_BLOCK_RE = re.compile(r"\*\*\* Begin Patch.*?(?:\*\*\* End Patch|\Z)", re.DOTALL)

# Older payloads can explicitly request sandbox permission. That request
# alone follows native approval; only a named forbidden capability is denied.
ESCALATION_KEYS = ("sandbox_permissions", "with_escalated_permissions", "escalated", "require_escalated")
ESCALATION_VALUES = {"require_escalated", "escalated", "true", "danger-full-access"}


@dataclass(frozen=True)
class Finding:
    """One refused reference in a text: the 1-based line, why it is refused, and the line as written.

    ``kind`` names a command domain, ``refused_script``, ``runs_on_load``
    or an action ``template``; targets and device categories are not refused.
    """

    line: int
    reason: str
    text: str
    kind: str = ""

    def key(self) -> tuple[str, str]:
        """Return what identifies the finding regardless of where it moved in the file."""
        return (self.reason, self.text.strip())


def rules_path() -> Path:
    """Return the image's refused-domain list; a source checkout (tests, development) uses policy/."""
    image_copy = IMAGE_DIR / "refused-domains.json"
    source = SOURCE_DIR / "refused-domains.json"
    return image_copy if image_copy.exists() or not source.exists() else source


def load_rules(path: Path | None = None) -> dict[str, Any]:
    """Return the refused-domain rules; the built-in strict list when the file is missing or unreadable.

    A file can add domains but never remove the built-in ones.
    """
    rules = json.loads(json.dumps(STRICT_RULES))
    try:
        data = json.loads((path or rules_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rules
    if not isinstance(data, dict):
        return rules
    for key in LIST_KEYS:
        extra = data.get(key)
        if isinstance(extra, list):
            rules[key] = sorted(set(rules[key]) | {str(item) for item in extra})
    return rules


class YamlBudgetError(ValueError):
    """The parsed second pass cannot finish within its fixed work budget."""


_scan_nodes = ContextVar("yaml_scan_nodes", default=None)
YAML_MAX_BYTES = 10 * 1024 * 1024
YAML_MAX_NODES = 1000000
INCLUDE_MAX_NODES = 1000000
INCLUDE_MAX_FILES = 3000
INCLUDE_MAX_BYTES = 16 * 1024 * 1024


class YamlCache:
    """Bound retained parse graphs by source bytes and entries, keyed by content hash."""
    def __init__(self):
        self.results = {}
        self.bytes = 0

    def get(self, digest):
        return self.results.get(digest)

    def put(self, digest, size, result):
        if size > 16 * 1024 * 1024:
            return
        while self.results and (len(self.results) >= 512 or self.bytes + size > 16 * 1024 * 1024):
            oldest = next(iter(self.results))
            old_size, _ = self.results.pop(oldest)
            self.bytes -= old_size
        self.results[digest] = (size, result)
        self.bytes += size


class ParseScan:
    """One deterministic operation; a watch may retain its bounded parse cache."""
    def __init__(self, cache=None):
        self.cache = cache if cache is not None else YamlCache()
        self.failures = {}


def scan_failures():
    scan = _scan_nodes.get()
    return dict(scan.failures) if scan is not None else {}


def has_yaml_content(text):
    """A comment-only/empty document is not an unknown dependency."""
    return any(line.strip() and not line.lstrip().startswith('#')
               and line.strip() not in ('---', '...') for line in text.splitlines())


def include_failure(source, reason, strict):
    message = f'{source}: {reason}'
    if strict:
        raise ValueError(message)
    scan = _scan_nodes.get()
    if scan is not None:
        scan.failures[source] = reason


def yaml_scan(function):
    """Share bounded parse results (including failures) for one hook/watch scan."""
    @wraps(function)
    def run(*args, **kwargs):
        if _scan_nodes.get() is not None:
            return function(*args, **kwargs)
        cache = getattr(args[0], '_yaml_cache', None) if args else None
        token = _scan_nodes.set(ParseScan(cache))
        try:
            return function(*args, **kwargs)
        finally:
            _scan_nodes.reset(token)
    return run


def yaml_result(text):
    scan = _scan_nodes.get()
    raw = text.encode('utf-8')
    digest = hashlib.sha256(raw).digest()
    cached = scan.cache.get(digest) if scan is not None else None
    if cached is not None:
        return cached[1]
    try:
        result = (_compose_yaml(text), '')
    except (YamlBudgetError, RecursionError):
        result = (None, 'YAML exceeds the bounded parsed check; review this file manually')
    if scan is not None:
        scan.cache.put(digest, len(raw), result)
    return result


def yaml_node(text: str) -> Any:
    return yaml_result(text)[0]


def _compose_yaml(text: str) -> Any:
    """Compose safe nodes, retaining !include/!secret tags without constructing objects.

    Node visits, nesting and input bytes are bounded independently of clocks. Invalid
    partial YAML falls back to the existing line rules, not a permissive loader.
    """
    if len(text.encode("utf-8")) > YAML_MAX_BYTES:
        raise YamlBudgetError("YAML byte budget exceeded")
    class Loader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self.visits, self.depth = 0, 0

        def compose_node(self, parent, index):
            self.visits += 1
            self.depth += 1
            try:
                if self.visits > YAML_MAX_NODES or self.depth > 64:
                    raise YamlBudgetError("YAML parse budget exceeded")
                return super().compose_node(parent, index)
            finally:
                self.depth -= 1
    loader = Loader(text)
    try:
        return loader.get_single_node()
    except yaml.YAMLError:
        return None
    finally:
        loader.dispose()


def yaml_mappings(node: Any):
    """Walk shared/cyclic aliases once, including merge-key mappings."""
    seen, todo = set(), [node]
    while todo:
        item = todo.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, yaml.MappingNode):
            yield item
            for key, value in item.value:
                todo.extend((key, value))
        elif isinstance(item, yaml.SequenceNode):
            todo.extend(item.value)


def yaml_digest(node: Any) -> str:
    """Hash resolved node content once per node; cyclic aliases never expand."""
    memo, visiting = {}, set()
    def digest(item):
        identity = id(item)
        if identity in memo:
            return memo[identity]
        if identity in visiting:
            return "cycle"
        visiting.add(identity)
        if isinstance(item, yaml.MappingNode):
            value = [(digest(k), digest(v)) for k, v in item.value]
        elif isinstance(item, yaml.SequenceNode):
            value = [digest(v) for v in item.value]
        else:
            value = item.value
        result = hashlib.sha256(json.dumps([item.tag, value], ensure_ascii=True).encode()).hexdigest()
        visiting.remove(identity)
        memo[identity] = result
        return result
    return digest(node)


def parsed_findings(text: str, rules: dict[str, Any], scripts: set[str] | frozenset[str]) -> list[Finding]:
    """Find semantic keys/values in flow, quoted and alias/merge spellings."""
    return node_findings(yaml_node(text), text, rules, scripts)


def is_definition(key, value, rules):
    return key in rules['runs_on_load_keys'] and (
        isinstance(value, (yaml.MappingNode, yaml.SequenceNode))
        or value.tag == 'tag:yaml.org,2002:null' or value.tag.startswith('!'))


def node_findings(node, text, rules, scripts):
    findings, lines = [], text.splitlines()
    for mapping in yaml_mappings(node):
        for key, value in mapping.value:
            if not isinstance(key, yaml.ScalarNode):
                continue
            name = key.value
            scalar = value.value.strip().lower() if isinstance(value, yaml.ScalarNode) and value.tag.startswith('tag:yaml.org,2002:') else ''
            number = key.start_mark.line + 1
            source = lines[number - 1] if number <= len(lines) else ''
            def add(reason, kind):
                findings.append(Finding(number, reason, source, kind))
            if is_definition(name, value, rules):
                add(f"a {name} definition, which runs on its own once loaded", 'runs_on_load')
            if name == 'platform' and scalar in rules['runs_on_load_platforms']:
                add(f"a {scalar} platform, which runs on its own once loaded", 'runs_on_load')
            if name in ('action', 'service', 'service_template'):
                if '{{' in scalar or '{%' in scalar or name == 'service_template':
                    add('a templated action', 'template')
                match = re.fullmatch(r'([a-z0-9_]+)\.([a-z0-9_]+)', scalar)
                if match:
                    domain, service = (x.strip().lower() for x in match.groups())
                    if domain in rules['reaches_anything']:
                        add(f"an action on {domain}, which can reach anything", domain)
                    elif domain == 'script' and service not in SCRIPT_SERVICES:
                        if service in scripts:
                            add(f"a call to script.{service}, which itself acts on a refused domain", 'refused_script')
            if name == 'entity_id':
                values = value.value if isinstance(value, yaml.SequenceNode) else [value]
                for target in values:
                    if isinstance(target, yaml.ScalarNode):
                        for entity in target.value.split(','):
                            entity = entity.strip().lower()
                            if entity.startswith('script.') and entity[7:] in scripts:
                                add(f"a call to {entity}, which itself acts on a refused domain", 'refused_script')
    return findings


def parsed_blocks(text: str, rules: dict[str, Any]) -> dict[str, tuple[int, int, str]]:
    """Definition hashes include resolved aliases, even when anchors live elsewhere."""
    lines, blocks = text.splitlines(), {}
    for mapping in yaml_mappings(yaml_node(text)):
        for key, value in mapping.value:
            if not isinstance(key, yaml.ScalarNode):
                continue
            scalar = value.value.strip().lower() if isinstance(value, yaml.ScalarNode) else ''
            definition = is_definition(key.value, value, rules)
            security = definition or (
                key.value == 'platform' and scalar in rules['runs_on_load_platforms']) or (
                key.value in ('action', 'service', 'service_template')
                and ('{{' in scalar or '{%' in scalar or key.value == 'service_template'))
            if security:
                line = key.start_mark.line + 1
                blocks['yaml:' + key.value + ':' + yaml_digest(value if definition else mapping)] = (
                    line, max(line, value.end_mark.line + 1), lines[line - 1])
    return blocks


@yaml_scan
def find_refused(text: str, rules: dict[str, Any], refused_scripts: frozenset[str] | set[str] = frozenset()) -> list[Finding]:
    """Find always-on commands, definitions/platforms, templated actions and indirect security calls.

    Device actions and targets follow the owner's normal approval flow. This
    tolerant line scan also works while an edit is incomplete.
    """
    anything = set(rules["reaches_anything"])
    platforms = set(rules["runs_on_load_platforms"])
    definitions = set(rules["runs_on_load_keys"])
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = ACTION_RE.match(line)
        if match:
            domain, service = (x.strip().lower() for x in match.groups())
            if domain in anything:
                findings.append(Finding(number, f"an action on {domain}, which can reach anything", line, domain))
            elif domain == "script" and service not in SCRIPT_SERVICES and service in refused_scripts:
                findings.append(Finding(number, f"a call to script.{service}, which itself acts on a refused domain", line,
                                        "refused_script"))
        else:
            platform = PLATFORM_RE.match(line)
            if platform and platform.group(1).lower() in platforms:
                findings.append(Finding(number, f"a {platform.group(1)} platform, which runs on its own once loaded", line,
                                        "runs_on_load"))
            definition = DEFINITION_KEY_RE.match(line)
            if definition and definition.group(1) in definitions:
                findings.append(Finding(number, f"a {definition.group(1)} definition, which runs on its own once loaded", line,
                                        "runs_on_load"))
        if TEMPLATE_ACTION_RE.match(line):
            findings.append(Finding(number, "a templated action", line, "template"))
        if not match and SCRIPT_TARGET_RE.match(line.lower()):
            for ref in (x.lower() for x in SCRIPT_REF_RE.findall(line)):
                if ref not in SCRIPT_SERVICES and ref in refused_scripts:
                    findings.append(Finding(number, f"a call to script.{ref}, which itself acts on a refused domain", line,
                                            "refused_script"))
                    break
    additions = parsed_findings(text, rules, refused_scripts)
    if yaml_result(text)[1]:
        additions.append(Finding(1, yaml_result(text)[1], '', 'parse_budget'))
    seen = {(f.line, f.reason, f.kind) for f in findings}
    findings.extend(f for f in additions if (f.line, f.reason, f.kind) not in seen)
    return findings


BLOCK_CHANGED = "a change to a block that holds a refused action"


@yaml_scan
def refused_blocks(text: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset()) -> dict[str, tuple[int, int, str]]:
    """Return {hash: (first line number, last line number, first line)} for each top-level block of this YAML text that holds a refused line.

    Definitions, platforms and templated actions stay hashed;
    blocks containing only unchanged calls then follow normal approval.
    A block starts at a line that begins in the first column (``- id: ...`` in
    automations.yaml, ``goodnight:`` in scripts.yaml, ``automation:`` in a
    package). Changing a trigger or a condition of an automation that already
    holds a refused action leaves the action line as it was, so the line
    findings see nothing new; the block's hash changes.
    """
    out: dict[str, tuple[int, int, str]] = {}
    block: list[str] = []
    start = 1
    all_findings = find_refused(text, rules, scripts)
    for number, line in enumerate(text.splitlines() + ["__end__"], start=1):
        if line[:1] and not line[:1].isspace() and not line.startswith("#") and block:
            body = "\n".join(item.rstrip() for item in block).strip()
            findings = [f for f in all_findings if start <= f.line < number]
            if body and any(finding.kind in {"runs_on_load", "template"} for finding in findings):
                out[hashlib.sha256(body.encode("utf-8")).hexdigest()] = (start, number - 1, block[0])
            block, start = [], number
        block.append(line)
    out.update(parsed_blocks(text, rules))
    if yaml_result(text)[1]:
        out[hashlib.sha256(text.encode()).hexdigest()] = (1, max(1, len(text.splitlines())), "YAML parsed-check budget exceeded")
    return out


@yaml_scan
def new_refused(before: str, after: str, rules: dict[str, Any], scripts: set[str] | frozenset[str] = frozenset()) -> list[Finding]:
    """Find newly added refused lines and changed blocks, without treating moved lines as new."""
    counts = Counter(item.key() for item in find_refused(before, rules, scripts))
    found = []
    for item in find_refused(after, rules, scripts):
        if counts[item.key()]:
            counts[item.key()] -= 1
        else:
            found.append(item)
    # Definition/platform/template-action bodies stay checked. Call-only blocks
    # follow normal approval when the call itself is unchanged.
    previous = refused_blocks(before, rules, scripts)
    for digest, (first, last, text) in refused_blocks(after, rules, scripts).items():
        if digest not in previous and not any(first <= item.line <= last for item in found):
            found.append(Finding(first, BLOCK_CHANGED, text))
    return found



@yaml_scan
def refused_scripts(texts: dict[str, str], rules: dict[str, Any], strict: bool = False) -> set[str]:
    """Resolve scripts from composed bodies; unknown script sources cannot approve edits."""
    blocks = {}
    sources = {rel: 'auto' for rel in texts if rel == 'scripts.yaml' or rel.startswith('scripts/')}
    for source, text in texts.items():
        node, error = yaml_result(text)
        if strict and (error or (node is None and re.search(r'\bscript\b', text) and has_yaml_content(text))):
            raise ValueError(f'{source}: script definitions could not be parsed; calls are refused')
        # Inspect composed keys, including quoted/escaped script keys. A text
        # prefilter cannot prove an uncomposed document has no script definitions.
        for mapping in yaml_mappings(node):
            for key, value in mapping.value:
                if not isinstance(key, yaml.ScalarNode) or key.value != 'script':
                    continue
                if isinstance(value, yaml.MappingNode):
                    for name, body in value.value:
                        if isinstance(name, yaml.ScalarNode):
                            blocks[name.value] = (body, text)
                elif isinstance(value, yaml.ScalarNode) and value.tag.startswith('!include'):
                    target = include_path(source, value.value)
                    if value.tag == '!include':
                        sources[target] = 'map'
                    elif value.tag in ('!include_dir_named', '!include_dir_merge_named'):
                        for rel in texts:
                            if rel.startswith(target + '/') and rel.lower().endswith(YAML_SUFFIXES):
                                sources[rel] = 'file' if value.tag == '!include_dir_named' else 'map'
    for rel, text in texts.items():
        if rel not in sources:
            continue
        node = yaml_node(text)
        if isinstance(node, yaml.MappingNode):
            keys = {key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)}
            if sources[rel] == 'file' or (sources[rel] == 'auto' and rel.startswith('scripts/') and 'sequence' in keys):
                blocks[Path(rel).stem] = (node, text)
            else:
                for key, value in node.value:
                    if isinstance(key, yaml.ScalarNode):
                        blocks[key.value] = (value, text)
        else:
            if strict and has_yaml_content(text):
                raise ValueError(f'{rel}: script definitions could not be parsed; calls are refused')
            # Preserve the line pass for partially edited script maps.
            current, block = '', []
            for line in text.splitlines() + ['__end__:']:
                key = re.match(r'^([a-z0-9_]+)\s*:\s*(?:#.*)?$', line)
                if key:
                    if current:
                        blocks[current] = '\n'.join(block)
                    current, block = key.group(1), []
                else:
                    block.append(line)
    refused: set[str] = set()
    while True:
        found = set()
        for name, body in blocks.items():
            # Classification needs the node, not source lines. Splitting the full
            # script-map text once per body makes an ordinary map quadratic.
            findings = node_findings(body[0], '', rules, refused) if isinstance(body, tuple) else find_refused(body, rules, refused)
            if any(f.kind != "parse_budget" for f in findings):
                found.add(name)
        if found <= refused:
            return refused
        refused |= found


def include_path(source: str, value: str) -> str:
    """Resolve only ordinary in-root includes, without ever opening credentials."""
    if value.startswith('/'):
        raise ValueError('absolute include path is not checked')
    rel = posixpath.normpath(posixpath.join(posixpath.dirname(source), value.rstrip('/')))
    if rel.startswith('../') or rel == '..' or any(part.startswith('.') for part in Path(rel).parts):
        raise ValueError('include path leaves ordinary configuration')
    if any(re.search(r'secret|credential|password|^auth\.json$|^tokens?\.', part, re.I) for part in Path(rel).parts):
        raise ValueError('credential includes are never read')
    return rel


@yaml_scan
def included_definitions(texts: dict[str, str], rules: dict[str, Any], strict: bool = False) -> dict[str, set[str]]:
    """Resolve include chains and definition directories; every member is hashed whole."""
    definitions: dict[str, set[str]] = {}
    roots = {}
    for rel, text in sorted(texts.items()):
        if '!include' in text and has_yaml_content(text):
            roots[rel] = yaml_node(text)
            if roots[rel] is None:
                include_failure(rel, 'include source could not be parsed', strict)
    todo = [(rel, node, frozenset()) for rel, node in roots.items()]
    seen = set()
    while todo:
        source, node, kinds = todo.pop()
        if node is None or (source, id(node), kinds) in seen:
            continue
        seen.add((source, id(node), kinds))
        if len(seen) > INCLUDE_MAX_NODES:
            include_failure(source, 'include graph node budget exceeded', strict)
            break
        if isinstance(node, yaml.ScalarNode) and node.tag.startswith('!include'):
            try:
                target = include_path(source, node.value)
            except ValueError:
                if strict:
                    raise ValueError(f'{source}: include cannot be checked safely') from None
                continue
            if node.tag == '!include':
                targets = [target]
            elif node.tag in ('!include_dir_named', '!include_dir_merge_named', '!include_dir_list', '!include_dir_merge_list'):
                targets = [rel for rel in texts if rel.startswith(target + '/') and rel.lower().endswith(YAML_SUFFIXES)]
            else:
                if strict:
                    raise ValueError(f'{source}: unsupported include tag')
                continue
            for rel in targets:
                if kinds:
                    definitions.setdefault(rel, set()).update(kinds)
                if rel in texts:
                    if rel not in roots:
                        roots[rel] = yaml_node(texts[rel])
                        if roots[rel] is None and has_yaml_content(texts[rel]):
                            include_failure(rel, 'included definition could not be parsed', strict)
                    todo.append((rel, roots[rel], kinds))
        elif isinstance(node, yaml.MappingNode):
            platform_kinds = {v.value for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == 'platform' and isinstance(v, yaml.ScalarNode) and v.value in rules['runs_on_load_platforms']}
            kinds = kinds | frozenset(platform_kinds)
            for key, value in node.value:
                extra = {key.value} if isinstance(key, yaml.ScalarNode) and key.value in rules['runs_on_load_keys'] else set()
                todo.append((source, value, kinds | frozenset(extra)))
        elif isinstance(node, yaml.SequenceNode):
            todo.extend((source, child, kinds) for child in node.value)
    return definitions


def included_finding(rel: str, kinds: set[str], text: str) -> Finding:
    return Finding(1, 'an included ' + '/'.join(sorted(kinds)) + ' definition',
                   text.splitlines()[0] if text.splitlines() else '', 'runs_on_load')


def config_yaml_texts(root: Path, max_bytes: int = 8 * 1024 * 1024) -> dict[str, str]:
    """Read a bounded ordinary configuration inventory, never links or secret tags."""
    texts = {}
    total = 0
    for parent, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in ('backups', 'node_modules', '__pycache__')
                         and not (Path(parent)/d).is_symlink())
        for name in sorted(names):
            if not name.lower().endswith(YAML_SUFFIXES) or name.lower() in ('secrets.yaml', 'secrets.yml'):
                continue
            if len(texts) >= 3000:
                raise ValueError('configuration inventory budget exceeded')
            rel = (Path(parent)/name).relative_to(root).as_posix()
            if (Path(parent)/name).is_symlink():
                continue
            try:
                include_path('', rel)
            except ValueError:
                continue  # Credential-named inventories are never opened.
            try:
                raw = read_config_bytes(root, rel, max_bytes)
            except ValueError:
                continue  # Unrelated oversized files cannot veto a bounded patch.
            total += len(raw)
            if total > 16 * 1024 * 1024:
                raise ValueError('configuration inventory byte budget exceeded')
            texts[rel] = raw.decode('utf-8', errors='replace')
    return texts


@yaml_scan
def extend_include_texts(texts: dict[str, str], root: Path, rules: dict[str, Any],
                         strict: bool = False, max_bytes: int = 512 * 1024,
                         raw_files: dict[str, bytes] | None = None) -> dict[str, str]:
    """Load ordinary referenced definitions even when their suffix is not YAML."""
    texts = dict(texts)
    loaded, total = set(texts), sum(len(text.encode()) for text in texts.values())
    for _ in range(64):
        added = False
        for rel in included_definitions(texts, rules, strict):
            if rel in loaded:
                continue
            loaded.add(rel)
            if len(loaded) > INCLUDE_MAX_FILES:
                include_failure(rel, 'include file budget exceeded', strict)
                return texts
            try:
                raw = read_config_bytes(root, rel, max_bytes)
            except FileNotFoundError:
                if strict:
                    raise ValueError(f'{rel}: included definition is missing') from None
                continue
            except (OSError, ValueError):
                if strict:
                    raise ValueError(f'{rel}: included definition is not an ordinary bounded file') from None
                continue
            total += len(raw)
            if total > INCLUDE_MAX_BYTES:
                include_failure(rel, 'include byte budget exceeded', strict)
                return texts
            texts[rel] = raw.decode('utf-8', errors='replace')
            if raw_files is not None:
                raw_files[rel] = raw
            added = True
        if not added:
            return texts
    include_failure(rel, 'include depth budget exceeded', strict)
    return texts


@yaml_scan
def config_refused_scripts(rules: dict[str, Any]) -> set[str]:
    texts = config_yaml_texts(CONFIG_ROOT)
    return refused_scripts(texts, rules)


PATCH_MAX_BYTES = 256 * 1024
CONFIG_MAX_BYTES = 2 * 1024 * 1024
HOOK_MAX_BYTES = 512 * 1024


def read_config_bytes(root: Path, rel: str, max_bytes: int = CONFIG_MAX_BYTES) -> bytes:
    """Read a bounded ordinary file through no-follow directory descriptors, never credentials."""
    parts = Path(rel).parts
    if (not parts or Path(rel).is_absolute() or any(p.startswith(".") for p in parts)
            or any(p.lower() in {"secrets.yaml", "secrets.yml"} for p in parts)):
        raise ValueError("not an ordinary configuration path")
    folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder)
            folder = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            info = os.fstat(fd)
            if info.st_size > max_bytes:
                raise ValueError(f'{rel}: larger than {max_bytes // 1024} KiB')
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"{rel}: not a bounded ordinary configuration file")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"{rel}: configuration file exceeds size limit")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(folder)


def patch_versions(text: str, cwd: str = "", extra_paths: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str, str]]:
    """Reconstruct YAML before/after in memory; an uncertain patch is refused, never executed.

    This accepts apply_patch's context hunks, add/delete and move forms. Each
    context must identify exactly one span after the preceding hunk. A move
    checks both locations and reads only its original, ordinary /config file.
    """
    if len(text.encode("utf-8")) > PATCH_MAX_BYTES:
        raise ValueError("patch exceeds size limit")
    match = PATCH_BLOCK_RE.search(text)
    if not match or not match.group().rstrip().endswith("*** End Patch"):
        raise ValueError("incomplete patch")
    if len(PATCH_BLOCK_RE.findall(text)) != 1:
        raise ValueError("multiple patch bodies are not checked as one edit")
    lines = match.group().splitlines()[1:-1]
    records: list[tuple[str, str, list[str]]] = []
    for line in lines:
        header = PATCH_FILE_RE.match(line)
        if header and header[1] != "Move to":
            records.append((header[1], header[2], []))
        elif records:
            records[-1][2].append(line)
        elif line.strip():
            raise ValueError("patch has no file header")
    out = []
    root = CONFIG_ROOT.absolute()
    def relative(name: str) -> str:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = Path(cwd or root) / candidate
        # Do not normalize away traversal or follow any link, even inside /config.
        if ".." in candidate.parts:
            raise ValueError("patch path traverses directories")
        rel = candidate.relative_to(root).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            raise ValueError("patch path is not ordinary configuration")
        for pattern, _ in PROTECTED_PATH_RES:
            if pattern.search(rel):
                raise ValueError("patch names a protected path")
        return rel
    total = 0
    seen: set[str] = set()
    if len(records) > 64:
        raise ValueError("patch file budget exceeded")
    for kind, name, body in records:
        moved = next((line.split(": ", 1)[1] for line in body if line.startswith("*** Move to: ")), name)
        if not (name.lower().endswith(YAML_SUFFIXES) or moved.lower().endswith(YAML_SUFFIXES)
                or (extra_paths and (relative(name) in extra_paths or relative(moved) in extra_paths))):
            continue
        source, destination = relative(name), relative(moved)
        if source in seen or destination in seen:
            raise ValueError("repeated file operations cannot be checked independently")
        seen.update((source, destination))
        if kind == "Add File":
            # Add File must not overwrite a real file or a symlink.
            try:
                read_config_bytes(root, source, HOOK_MAX_BYTES)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("added file already exists")
            before = ""
        else:
            before = read_config_bytes(root, source, HOOK_MAX_BYTES).decode("utf-8")
        destination_before = before
        if source != destination:
            if kind != "Update File":
                raise ValueError("only an update may move a file")
            try:
                destination_before = read_config_bytes(root, destination, HOOK_MAX_BYTES).decode("utf-8")
            except FileNotFoundError:
                destination_before = ""
        total += len(before.encode("utf-8"))
        if total > 8 * CONFIG_MAX_BYTES:
            raise ValueError("patch source budget exceeded")
        if kind == "Delete File":
            out.append((destination, before, ""))
            continue
        if kind == "Add File":
            if any(not line.startswith("+") for line in body):
                raise ValueError("invalid added-file body")
            out.append((destination, "", "\n".join(line[1:] for line in body) + "\n"))
            continue
        original = before.splitlines()
        updated: list[str] = []
        cursor = 0
        hunks: list[list[str]] = []
        for line in body:
            if line.startswith("*** Move to:"):
                continue
            if line.startswith("@@"):
                hunks.append([])
            elif line == "*** End of File":
                if not hunks:
                    raise ValueError("invalid end marker")
                hunks[-1].append(line)
            elif hunks and line[:1] in {" ", "+", "-"}:
                hunks[-1].append(line)
            else:
                raise ValueError("invalid patch hunk")
        for hunk in hunks:
            at_end = "*** End of File" in hunk
            hunk = [line for line in hunk if line != "*** End of File"]
            old = [line[1:] for line in hunk if line[0] in " -"]
            new = [line[1:] for line in hunk if line[0] in " +"]
            # Linear KMP matching avoids quadratic context scans that could hit
            # Codex's fail-open hook timeout on a large, repetitive YAML file.
            positions = []
            if not old:
                positions = [len(original)] if at_end or not original else []
            else:
                prefix = [0] * len(old)
                length = 0
                for n in range(1, len(old)):
                    while length and old[n] != old[length]:
                        length = prefix[length - 1]
                    if old[n] == old[length]:
                        length += 1
                    prefix[n] = length
                length = 0
                for n in range(cursor, len(original)):
                    while length and original[n] != old[length]:
                        length = prefix[length - 1]
                    if original[n] == old[length]:
                        length += 1
                    if length == len(old):
                        if not at_end or n + 1 == len(original):
                            positions.append(n + 1 - length)
                            if len(positions) > 1:
                                break
                        length = prefix[length - 1]
            if len(positions) != 1:
                raise ValueError("patch context is missing or ambiguous")
            index = positions[0]
            updated.extend(original[cursor:index]); updated.extend(new)
            cursor = index + len(old)
        if not hunks and source == destination:
            raise ValueError("empty update")
        updated.extend(original[cursor:])
        after = "\n".join(updated) + ("\n" if before.endswith("\n") else "")
        out.append((destination, before, after))
        if source != destination:
            # A harmless edit to a text file may plant its unchanged body in YAML.
            # Judge what is new at the destination too, including absent files.
            out.append((destination, destination_before, after))
    return out


def parse_patch(text: str) -> list[tuple[str, str]]:
    """Return (path, added lines) for each file in an apply_patch body; moved files count under their new path."""
    files: list[tuple[str, list[str]]] = []
    inside = False
    for raw in text.splitlines():
        if raw.startswith(PATCH_BEGIN):
            inside = True
            continue
        if not inside:
            continue
        if raw.startswith("*** End Patch"):
            inside = False
            continue
        header = PATCH_FILE_RE.match(raw)
        if header:
            kind, path = header.groups()
            if kind == "Move to" and files:
                files[-1] = (path, files[-1][1])
            else:
                files.append((path, []))
            continue
        if files and raw.startswith("+"):
            files[-1][1].append(raw[1:])
    return [(path, "\n".join(added)) for path, added in files]


def _strings(value: Any) -> list[str]:
    """Return every string inside a JSON value, in order; a list of words (an argv) is also joined into one."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        out = [text for item in value for text in _strings(item)]
        if value and all(isinstance(item, str) for item in value):
            out.append(" ".join(value))
        return out
    return []


def asks_for_escalation(tool_input: Any) -> bool:
    """Return whether a tool call asks to run outside the sandbox (network, paths outside /config)."""
    if not isinstance(tool_input, dict):
        return False
    for key in ESCALATION_KEYS:
        value = tool_input.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() in ESCALATION_VALUES):
            return True
    return False


# These are security restrictions, independent of any retired device category option.
REMOTE_DEFINITIONS = ("shell_command", "rest_command", "command_line", "python_script", "pyscript", "template")
FULL_ACCESS_RE = re.compile(r"danger[-_]?full[-_]?access|dangerFullAccess|--dangerously-bypass-approvals-and-sandbox|--yolo\b", re.IGNORECASE)

HA_REMOTE_FAMILIES = frozenset(("addon", "addons", "add-on", "add-ons", "host", "os", "core",
                                "hardware", "network", "backup", "backups", "apps"))
HA_ALWAYS_FAMILIES = frozenset(("supervisor", "su"))
SHELL_ASSIGNMENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*=")
# A conservative text floor also catches shell constructs the parser cannot place.
# The four documented value-taking global flags may precede the family.
HA_REMOTE_TEXT_RE = re.compile(
    r"\bha\s+(?:(?:--(?:log-level|api-token|endpoint|config)(?:=\S+|\s+\S+)|-\S+)\s+)*"
    r"(?:add-?ons?|host|os|core|hardware|network|backups?|apps)\b", re.IGNORECASE)
# Flag values and shell syntax can obscure the exact argument boundaries. The
# always-refused families remain protected even when they follow such values.
HA_SHELL_WORD_RE = re.compile(r"\bh[\"'\\]*a\b", re.IGNORECASE)
HA_UNKNOWN_FLAG_RE = re.compile(
    r"--(?:log-level|api-token|endpoint|config)(?:=|\s+)"
    r"[^\s;]*(?:[\"'`]|\$\()", re.IGNORECASE)


def _ha_unknown_flag(text: str) -> bool:
    """Conservatively refuse a quoted or computed global-flag value."""
    for line in text.splitlines():
        word = HA_SHELL_WORD_RE.search(line)
        if word and HA_UNKNOWN_FLAG_RE.search(line, word.end()):
            return True
    return False


def _shell_substitutions(text: str) -> tuple[str, list[str]]:
    """Mask literal shell substitutions while retaining their commands for inspection."""
    out, nested = [], []
    i, quote = 0, ''
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text) and quote != "'":
            out.append(text[i:i + 2]); i += 2; continue
        if ch == "'" and quote != '"':
            quote = '' if quote == "'" else "'"
        elif ch == '"' and quote != "'":
            quote = '' if quote == '"' else '"'
        if quote != "'" and text.startswith('$(', i):
            depth, end, inner_quote = 1, i + 2, ''
            while end < len(text) and depth:
                if text[end] == '\\' and end + 1 < len(text) and inner_quote != "'":
                    end += 2; continue
                if text[end] == "'" and inner_quote != '"':
                    inner_quote = '' if inner_quote == "'" else "'"
                elif text[end] == '"' and inner_quote != "'":
                    inner_quote = '' if inner_quote == '"' else '"'
                elif not inner_quote and text[end] == '(':
                    depth += 1
                elif not inner_quote and text[end] == ')':
                    depth -= 1
                end += 1
            if not depth:
                nested.append(text[i + 2:end - 1]); out.append('SUBSTITUTION'); i = end; continue
        if quote != "'" and ch == '`':
            end = text.find('`', i + 1)
            if end >= 0:
                nested.append(text[i + 1:end]); out.append('SUBSTITUTION'); i = end + 1; continue
        out.append(ch); i += 1
    return ''.join(out), nested


def _ha_supervisor_command(text: str, depth: int = 0) -> bool:
    """Find ha su/supervisor in a simple command, without executing shell text."""
    if depth > 6:
        return False
    masked, nested = _shell_substitutions(text.replace('\\\n', ' '))
    if any(_ha_supervisor_command(item, depth + 1) for item in nested):
        return True
    lexer = shlex.shlex(masked, posix=True, punctuation_chars=';&|()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = '#'
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    segments, segment = [], []
    for token in tokens:
        if token and all(ch in ';&|()\n' for ch in token):
            if segment:
                segments.append(segment); segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    for segment in segments:
        index = 0
        while index < len(segment):
            word = segment[index].lower()
            if word == 'eval' and index + 1 < len(segment):
                if _ha_supervisor_command(' '.join(segment[index + 1:]), depth + 1):
                    return True
                break
            if SHELL_ASSIGNMENT_RE.match(segment[index]) or word in ('if', 'then', 'do', '!', '{', 'exec', 'time', 'nohup'):
                index += 1; continue
            if word in ('sudo', 'env', 'command', 'xargs', 'timeout', 'watch', 'stdbuf', 'nice'):
                index += 1
                while index < len(segment) and segment[index].startswith('-'):
                    flag = segment[index]
                    index += 1
                    takes_value = ((word == 'sudo' and flag in ('-u', '--user', '-g', '--group'))
                                   or (word == 'env' and flag in ('-u', '--unset', '-C', '--chdir'))
                                   or (word == 'xargs' and flag in ('-n', '-I', '-P', '-s'))
                                   or (word == 'watch' and flag in ('-n', '--interval'))
                                   or (word == 'timeout' and flag in ('-s', '--signal', '-k', '--kill-after'))
                                   or (word == 'nice' and flag in ('-n', '--adjustment')))
                    if takes_value and index < len(segment):
                        index += 1
                if word == 'timeout' and index < len(segment):
                    index += 1
                continue
            break
        if index >= len(segment):
            continue
        program = segment[index].lower().rsplit('/', 1)[-1]
        if program == 'find' and '-exec' in segment[index + 1:]:
            start = segment.index('-exec', index + 1) + 1
            if _ha_supervisor_command(shlex.join(segment[start:]), depth + 1):
                return True
        if program == 'busybox' and index + 1 < len(segment):
            index += 1; program = segment[index].lower()
        if program in ('sh', 'bash', 'zsh'):
            for j in range(index + 1, len(segment)):
                if segment[j].startswith('-') and 'c' in segment[j][1:]:
                    j += 1
                    if j < len(segment) and segment[j] == '--':
                        j += 1
                    if j < len(segment) and _ha_supervisor_command(segment[j], depth + 1):
                        return True
                    break
        if program in ('python', 'python3') and index + 2 < len(segment) and segment[index + 1] == '-c':
            try:
                tree = ast.parse(segment[index + 2])
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'os'
                            and node.func.attr == 'system' and node.args and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and _ha_supervisor_command(node.args[0].value, depth + 1)):
                        return True
            except (SyntaxError, ValueError):
                pass
        if program != 'ha' or (segment[index].lower() != 'ha' and not segment[index].lower().endswith('/ha')):
            continue
        index += 1
        while index < len(segment) and segment[index].startswith('-'):
            flag = segment[index].lower(); index += 1
            if flag == '--':
                break
            if flag in ('--log-level', '--api-token', '--endpoint', '--config') and index < len(segment):
                index += 1
        if index < len(segment) and segment[index].lower() in HA_ALWAYS_FAMILIES:
            return True
    return False


def _ha_command(text: str, remote_on: bool, depth: int = 0) -> bool:
    """Recognize literal HA invocations in command position, including simple shell wrappers.

    This is a text check, not a shell interpreter: computed commands still depend on the sandbox.
    """
    if depth > 3:
        return False
    lexer = shlex.shlex(text.replace("\\\n", " "), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        # An unfinished quote must not turn a recognizable command into an escape.
        lexer = shlex.shlex(text.replace("\\\n", " ").replace('"', '').replace("'", ""),
                            posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError:
            return False
    segments = []
    segment = []
    for token in tokens:
        if token and all(char in ";&|()\n" for char in token):
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    for segment in segments:
        index = 0
        while index < len(segment):
            word = segment[index]
            if SHELL_ASSIGNMENT_RE.match(word):
                index += 1
            elif word.lower() in ("env", "sudo", "command", "xargs"):
                index += 1
                while index < len(segment) and segment[index].startswith("-"):
                    index += 1
            else:
                break
        if index >= len(segment):
            continue
        program = segment[index].lower()
        if program.rsplit("/", 1)[-1] in ("sh", "bash") and index + 2 < len(segment) and segment[index + 1] == "-c":
            if _ha_command(segment[index + 2], remote_on, depth + 1):
                return True
        if program != "ha" and not (program.startswith("/") and program.endswith("/ha")):
            continue
        index += 1
        while index < len(segment) and segment[index].startswith("-"):
            flag = segment[index].lower()
            index += 1
            if flag in ("--log-level", "--api-token", "--endpoint", "--config") and index < len(segment):
                index += 1
        if index < len(segment):
            family = segment[index].lower()
            if remote_on and family in HA_REMOTE_FAMILIES:
                return True
    return False


def _policy_restriction(text: str) -> str:
    """Name protected app settings/rules/policy in either a patch path or command text.

    Like the approval adapter, this is a conservative literal-path check, not
    a shell interpreter. Computed/obfuscated paths still require the sandbox.
    """
    plain = re.sub(r"[\\'\"]", "", text)
    plain = re.sub(r"/{2,}", "/", plain)
    if re.search(r"/data/options\.json", plain, re.IGNORECASE):
        return "changes the add-on's settings"
    if re.search(r"schedules\.sqlite3(?:-(?:journal|wal|shm))?|/data/sched[^/\s]*[*?\[]", plain, re.IGNORECASE):
        return "accesses the protected schedule database"
    if re.search(r"worker_api_token", plain, re.IGNORECASE):
        return "accesses the add-on's private credential"
    if re.search(r"codex-home/.*(?:AGENTS\.md|house-rules)|ha_codex/.*(?:AGENTS\.md|house-rules)", plain, re.IGNORECASE):
        return "changes the add-on's rules"
    if re.search(r"/share/ha_|ha_codex/(?:policy|agents|house)|codex-home/.*(?:config\.toml|[?*\[])|/etc/codex/(?:requirements|managed_config)", plain, re.IGNORECASE):
        return "changes the add-on's permission policy"
    return ""


def _python_definition(path: str) -> str:
    match = re.search(r"(?:^|[/\s'\"])(python_scripts|pyscript)/", path)
    return ("python_script" if match[1] == "python_scripts" else "pyscript") if match else ""


@yaml_scan
def check_tool_call(event: str, payload: Any, remote_on: bool, rules: dict[str, Any]) -> str:
    """Return why the hook refuses this tool call or permission request, or an empty string to let it through.

    ``event`` is the Codex hook event (``PreToolUse`` or ``PermissionRequest``);
    ``payload`` is the hook's input. Nothing here ever asks: the answer is a
    refusal or no opinion, and with no opinion the usual approval flow runs.
    """
    data = payload if isinstance(payload, dict) else {}
    event = str(data.get("hook_event_name") or event or "")
    tool_input = data.get("tool_input", data.get("input", data))
    if remote_on and (event == "PermissionRequest" or asks_for_escalation(tool_input)):
        if not isinstance(tool_input, dict) or not any(
                tool_input.get(key) for key in ("command", "patch", "path", "file_path", "changes")):
            return "This permission request does not name an action the command check can review; it is refused."
    # An ordinary PermissionRequest is not an escalation to full access. The
    # unchanged hook returns exit 0 with no output, never an allow decision.
    if remote_on:
        rules = {**rules, "runs_on_load_keys": sorted(set(rules["runs_on_load_keys"]) | {"template"})}
    texts = _strings(tool_input)
    if len(texts) > 128 or sum(len(text) for text in texts) > 1024 * 1024:
        return "This tool input exceeds the command check's size budget; it is refused."
    for text in texts:
        if remote_on:
            if FULL_ACCESS_RE.search(text):
                return "This request enables full access, which is never allowed."
            restriction = _policy_restriction(text)
            if restriction:
                return f"This request {restriction}, which is never allowed."
            definition = _python_definition(text)
            if definition:
                return f"This request names a {definition} definition file, which Codex may not write or edit."
        if PATCH_BEGIN in text:
            if len(text.encode("utf-8")) > PATCH_MAX_BYTES:
                return "This patch exceeds the command check's size budget; it is refused."
            try:
                added_files = parse_patch(text)
                versions = patch_versions(text, str(data.get("cwd") or "")) if remote_on else []
                # Reserve parsing of proposed content before scanning unrelated inventory.
                if remote_on:
                    for path, before, after in versions:
                        yaml_node(before)
                        yaml_node(after)
                for path, added in added_files:
                    yaml_node(added)
                inventory = extend_include_texts(config_yaml_texts(CONFIG_ROOT), CONFIG_ROOT, rules, strict=remote_on)
                scripts = refused_scripts(inventory, rules, strict=remote_on)
                for path, added in added_files:
                    for pattern, why in PROTECTED_PATH_RES:
                        if pattern.search(path):
                            return f"This edit {why}, which is never allowed."
                    if path.lower().endswith(YAML_SUFFIXES):
                        found = find_refused(added, rules, scripts)
                        if found:
                            return f"This edit to {path} adds {found[0].reason}. A person must add this change outside Codex."
                if remote_on:
                    original = dict(inventory)
                    inventory.update({path: after for path, before, after in versions})
                    definitions = included_definitions(original, rules, strict=True)
                    for path, kinds in included_definitions(inventory, rules, strict=True).items():
                        definitions.setdefault(path, set()).update(kinds)
                    # Known includes are checked regardless of filename suffix, including
                    # newly proposed include paths and move destinations.
                    # Reconstruct once more only if a non-YAML include needs checking.
                    if any(not path.lower().endswith(YAML_SUFFIXES) for path in definitions):
                        versions = patch_versions(text, str(data.get("cwd") or ""), set(definitions))
                    for path, before, after in versions:
                        if path in definitions and before != after:
                            return f"This edit to {path} changes {included_finding(path, definitions[path], after).reason}. A person must add it outside Codex."
                        found = new_refused(before, after, rules, scripts)
                        if found:
                            reason = found[0].reason
                            if reason == BLOCK_CHANGED:
                                body = find_refused(after, rules, scripts)
                                reason = next((item.reason for item in body if item.kind in {"runs_on_load", "template"}), reason)
                            return f"This edit to {path} makes {reason}. A person must add this change outside Codex."
            except (OSError, ValueError, UnicodeError) as exc:
                # ValueError messages here are worker-authored paths/reasons, never bytes read from a file.
                detail = str(exc) if isinstance(exc, ValueError) else 'configuration read failed'
                paths = ", ".join(path for path, _ in parse_patch(text))[:256]
                return f"This edit to {paths or 'the configuration'} cannot be checked safely: {detail}; it is refused."
            # The rest of a command that carries a patch (a heredoc) is still a command.
            text = PATCH_BLOCK_RE.sub(" ", text)
        if remote_on:
            definition = re.search(r"\b(" + "|".join(REMOTE_DEFINITIONS) + r")\s*:", text)
            if definition:
                return f"This command names a {definition[1]} definition, which Codex may not write or edit."
        for pattern, why in PROTECTED_COMMAND_RES:
            if pattern.search(text):
                return f"This command {why}, which is never allowed."
        plain_ha = re.sub(r"[\"'`]", "", text.replace("\\\n", " "))
        if (_ha_supervisor_command(text)
                or (remote_on and (HA_REMOTE_TEXT_RE.search(plain_ha) or _ha_unknown_flag(text)))
                or _ha_command(text, remote_on)):
            return "This command talks to the Home Assistant Supervisor, which is never allowed."
    return ""
