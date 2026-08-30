# Web UI fidelity ledger

## Source concepts

- Primary workspace: `docs/design/web-main-concept.png`
- Approval state: `docs/design/web-approval-concept.png`
- Native concept size: 1620 × 970

## Comparison ledger

| Check | Concept evidence | Implementation evidence | Status |
|---|---|---|---|
| App anatomy | quiet top bar; session rail; central work log; right inspector; bottom status | `index.html` app shell and `app.css` three-column grid | matched structurally |
| Palette | paper white, navy ink, cornflower blue, mint, apricot, coral, lilac | named CSS tokens under `:root`; no gradients or dark-mode override | matched by tokens |
| Tool path | blue illustrated vertical path with expandable tool steps | `.activity::before`, `.tool-step`, per-tool SVG icons and disclosure states | matched structurally |
| Typography | rounded Chinese UI text plus precise monospace code | Microsoft YaHei UI / PingFang SC fallback; Cascadia Code / Consolas fallback | matched by available local fonts |
| Mascot | small blue assistant in brand and Agent states | generated transparent `mascot.png`, used at 34–100 px | matched asset family |
| Context inspector | four paper-tab layers; working-memory ledger; 75% → 50% control | dynamic context segments, layer rows, real counts and compact command | matched functionally |
| Approval | inline apricot ledger connected to task path; reject and allow-once only | dynamic `tool.approval_required` renderer and synchronous `ApprovalBroker` | matched functionally |
| Git inspector | remote, branch, commits, files, State Gate evidence | populated from approval Preflight context using `textContent` | matched functionally |
| Responsive behavior | session and inspector collapse into drawers | breakpoints at 1180 px and 820 px | implemented; screenshot pending |

## Visible-copy diff

The implementation preserves the approved product labels and actions. Paths, commit hashes, counts, tool summaries and Context usage are intentionally dynamic rather than copied from the concept. A small empty-session welcome is the only additional state; it disappears as soon as the first event is recorded. The earlier unapproved `当前会话` pretitle was removed.

## Verification status

- Static assets: HTTP 200.
- Real Uvicorn WebSocket upgrade: passed.
- Simulated turn through WebSocket: passed.
- Real Gemini read-only turn through WebSocket: passed.
- Automated desktop/mobile screenshot comparison: blocked because the in-app browser could not verify its administrator-enforced localhost access policy. The check was retried through the documented browser path only; no alternate browser or security bypass was used.
- Manual Codex browser preview: opened at `http://127.0.0.1:8765/` for user review.

Agency-signoff fidelity must not be claimed until a rendered desktop screenshot and mobile screenshot can be inspected alongside the accepted concept with `view_image`.
