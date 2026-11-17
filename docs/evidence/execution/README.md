# Execution evidence

See the [acceptance report](../../en/development/execution-acceptance.md) for scope and limitations.

- `macos-*-samples.json` retain every sample from the identified source revision. Earlier revision samples remain under `945bdfa/`; do not combine revisions.
- `macos-codex-instructions.json` and `instruction-summary.json` contain the independent frozen Codex task run and manual intent observations. The earlier Anthropic authentication failure remains separately recorded.
- `maintenance.json` distinguishes movement, retained protection, deleted modules and rewritten lines.
- `native-60df626/` contains exact-head CI probe results, artifact identities and native JUnit reports. The `.xml.gz` files decompress to the original, unmodified JUnit bytes; the summary records their SHA-256 digests. Compression preserves original whitespace and Windows line endings. Windows probe JSON is also retained verbatim as `.raw.json.gz`; its readable `.json` copy only converts CRLF to LF.

No report here accepts native MXC isolation. A successful file broker check or a refused native launch is not proof of native isolation.
