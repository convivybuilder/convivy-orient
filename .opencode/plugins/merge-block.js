// Merge-block plugin — OpenCode port of the ARCH-100 PreToolUse(Bash) guard
// (agent-platform/tools/block-pr-merge.sh). Substrate ARCH-020/100/103/133.
//
// Agents never merge PRs, and OpenCode trial sessions never push to main —
// even if the session was told "ship it". Surface the PR URL; the operator merges in
// the GitHub UI.
//
// Port notes vs the bash original:
//   - Same MENTION vs INVOCATION semantics: matches are anchored to a
//     command-segment boundary (start-of-string or after ; & ( ), so prose
//     that mentions a merge string passes while an invocation is blocked.
//     Pipe '|' is deliberately NOT a separator (you never pipe INTO a merge,
//     and '|' as a separator false-positives on grep alternation).
//   - The protect-main branch applies UNCONDITIONALLY here (the bash original
//     gates it on AGENTCTL_RUN_ID). Trial rule: every OpenCode session is
//     PR-only — no direct push to main, steward included (the trial design's
//     PR+CI-only rule).
//   - denyReason() is a pure function exposed as MergeBlock.denyReason for
//     plain-node testing. It must NOT be a module-level export: OpenCode
//     treats every export as a plugin factory and dies evaluating the
//     non-plugin return value (hit live during setup, 2026-06-11).

const SEG = "(^|[;&(]) *";

function normalize(cmd) {
  // tab -> space, newline -> ';' (a real separator; a space would erase the
  // boundary and let a merge on its own line slip past the anchor).
  return String(cmd).replace(/\t/g, " ").replace(/\n/g, ";");
}

function denyReason(cmd) {
  const norm = normalize(cmd);

  // 1. The gh CLI merge subcommand as an actual command (segment-anchored).
  if (new RegExp(`${SEG}gh +pr +merge`).test(norm)) {
    return (
      "BLOCKED by ARCH-020 (hard guard): agents never merge PRs. 'ship it' / " +
      "'merge it' is NOT authorization to run the merge yourself — surface " +
      "the PR as ready (post the URL) and let the operator merge it in the GitHub UI."
    );
  }

  // 2. An actual api/curl/wget call (segment-anchored) hitting a merge
  //    endpoint. Both conditions required, so a prose token alone passes.
  if (
    new RegExp(`${SEG}(gh +api|curl|wget)`).test(norm) &&
    /\/pulls\/[0-9]+\/merge|mergePullRequest/.test(norm)
  ) {
    return (
      "BLOCKED by ARCH-020 (hard guard): agents never merge PRs (REST/GraphQL " +
      "merge endpoint). Surface the PR as ready; the operator merges in the GitHub UI."
    );
  }

  // 3. Any push to main. The `main` token must be a refspec word in the same
  //    push segment: matches `origin main`, `HEAD:main`, `main:main`, `:main`
  //    (delete), `+main` (force). `[^;&|]*` keeps the match inside one push
  //    segment. `main-feature` / `mainline` are NOT matched. A bare `git push`
  //    (current branch -> upstream) is not caught — work on feature branches;
  //    documented residual, same as the bash original.
  if (new RegExp(`${SEG}git +push[^;&|]*[ :+]main([ :;&]|$)`).test(norm)) {
    return (
      "BLOCKED (ARCH-133 trial rule, protect-main): OpenCode trial sessions " +
      "are PR-only. Push to a feature branch and open a PR — CI gates it and " +
      "the operator merges."
    );
  }

  return null;
}

export const MergeBlock = async () => ({
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "bash") return;
    const reason = denyReason(output?.args?.command ?? "");
    if (reason) throw new Error(reason);
  },
});

MergeBlock.denyReason = denyReason;
