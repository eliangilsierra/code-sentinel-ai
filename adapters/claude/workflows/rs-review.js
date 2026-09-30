// Orchestration of a review run: finders per job, deduplication, then adversarial verification.
// Arguments come from `review-ctx prepare`: { run, tier, verify, jobs: [{ id, lens }] }.
export const meta = {
  name: 'rs-review',
  description: 'Evidence-gated review: finders per job followed by adversarial verification',
}

if (!args || !args.run || !Array.isArray(args.jobs)) {
  throw new Error('rs-review requires args { run, verify, jobs }')
}

const { run, jobs, verify } = args

const SEVERITIES = ['important', 'nit', 'pre_existing', 'question']

const finderResult = {
  type: 'object',
  required: ['findings'],
  additionalProperties: false,
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'sev', 'lens'],
        additionalProperties: false,
        properties: {
          id: { type: 'string' },
          sev: { enum: SEVERITIES },
          lens: { type: 'string' },
        },
      },
    },
  },
}

const verifierResult = {
  type: 'object',
  required: ['id', 'verdict'],
  additionalProperties: false,
  properties: {
    id: { type: 'string' },
    verdict: { enum: ['confirmed', 'refuted', 'unverifiable'] },
  },
}

function needsVerification(policy, finding) {
  switch (policy) {
    case 'candidate':
    case 'all':
      return finding.sev === 'important' || finding.sev === 'pre_existing'
    case 'important':
      return finding.sev === 'important'
    case 'important+security':
      return (
        finding.sev === 'important' ||
        (finding.sev === 'pre_existing' && finding.lens === 'security')
      )
    default:
      return false
  }
}

phase('find')
const found = await pipeline(jobs, (job) =>
  agent(
    `RUN=${run} JOB=${job.id}\n` +
      `Read .git/code-sentinel/runs/${run}/jobs/${job.id}.md and follow it. ` +
      `Record every candidate with review-ctx emit --run ${run}.`,
    { label: job.id, agentType: 'code-sentinel:rs-finder', schema: finderResult },
  ),
)

const seen = new Set()
const candidates = []
for (const result of found.filter(Boolean)) {
  for (const finding of result.findings) {
    if (!seen.has(finding.id)) {
      seen.add(finding.id)
      candidates.push(finding)
    }
  }
}

const selected = candidates.filter((finding) => needsVerification(verify, finding))

phase('verify')
const verdicts = await pipeline(selected, (finding) =>
  agent(
    `RUN=${run}\nVerify finding ${finding.id}: run review-ctx show --run ${run} ${finding.id}, ` +
      'try to refute it and record the verdict with review-ctx verdict.',
    { label: finding.id, agentType: 'code-sentinel:rs-verifier', schema: verifierResult },
  ),
)

return {
  run,
  jobs: jobs.length,
  jobsFailed: found.filter((result) => !result).length,
  found: candidates.length,
  verified: verdicts.filter(Boolean).length,
  verificationsFailed: verdicts.filter((result) => !result).length,
}
