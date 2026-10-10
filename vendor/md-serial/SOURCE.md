# Vendored: md-serial

`md-serial.js`, `render.js` and `editor.js` are byte-for-byte copies of
`scripts/md-serial/` in the `convivy/knowledge` repository, at commit
`d31622cbaeb4fb0e637b61beffbf05842b83e56d` (md-serial's own last change there is
`867daf16`). md-serial is MIT-licensed and has no dependencies.

Orient holds no clone of that repository, so the knowledge-base editor
(`kb_editor.py`) loads these copies from `/editor/js/<name>`, a full-tier route in
`server.py`. `tests/md_serial_stub/test-helpers.js` is md-serial's own DOM stub
from the same commit, used only by the tests.

Do not edit these files here. To take a newer md-serial, copy the three files
and the stub from a newer knowledge commit, update the commit above, and run
`tests/test_kb_editor_md_serial.py`, which runs the round-trip corpus over them.
