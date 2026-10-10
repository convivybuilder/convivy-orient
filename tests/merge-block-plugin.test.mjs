import { MergeBlock } from '../.opencode/plugins/merge-block.js';
const denyReason = MergeBlock.denyReason;
const cases = [
  ['gh pr merge 123 --squash', true],
  ['ls; gh pr merge 5', true],
  ['echo hi\ngh pr merge 3', true],
  ['echo gh pr merge', false],
  ["git commit -m 'do not gh pr merge'", false],
  ['curl -X PUT https://api.github.com/repos/x/y/pulls/5/merge', true],
  ['gh api repos/x/y/pulls/12/merge -X PUT', true],
  ['echo /pulls/5/merge', false],
  ['grep -E "a\\|gh pr merge" file.txt', false],
  ['git push origin main', true],
  ['git push origin HEAD:main', true],
  ['git push --force origin +main', true],
  ['git push origin feature-main', false],
  ['git push origin mainline', false],
  ['git push origin mybranch', false],
  ['git push origin x | grep main', false],
  ['gh pr view 5 && gh pr checks 5', false],
];
let fail = 0;
for (const [cmd, expectBlock] of cases) {
  const blocked = denyReason(cmd) !== null;
  if (blocked !== expectBlock) { fail++; console.log('FAIL', JSON.stringify(cmd), 'expected', expectBlock, 'got', blocked); }
}
console.log(fail === 0 ? `all ${cases.length} cases pass` : `${fail} failures`);
