// The prior-art block (`prior_art` on /generate and on POST /steps with
// `research: true`; engine/silkscreen/prior_art.py PriorArtResult.as_dict),
// reduced to what a compact card shows: the top projects, each with its
// licence and a link, and the status in words when there are none.

/** How many projects the card lists. The brief the designer saw uses 3 too
    (PriorArtResult.brief_text limit=3), so the card shows what was used. */
export const MAX_PRIOR_ART_PROJECTS = 3

const STATUS_TEXT = {
  found: 'open-source projects that already build this',
  none_found: 'searched GitHub; nothing relevant came back',
  rate_limited: 'GitHub rate-limited the search (set GITHUB_TOKEN)',
  unavailable: 'GitHub could not be searched',
}

/** Only an https link to github.com is rendered as a link: the URL comes
    from the service, and the service got it from the network. */
export function safeRepoUrl(value) {
  const url = String(value ?? '')
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' && parsed.hostname === 'github.com' ? parsed.href : ''
  } catch {
    return ''
  }
}

export function readPriorArt(result) {
  const block = result?.prior_art
  if (!block || typeof block !== 'object') return null
  const status = String(block.status ?? '')
  const projects = (Array.isArray(block.projects) ? block.projects : [])
    .slice(0, MAX_PRIOR_ART_PROJECTS)
    .map((project) => {
      const repo = project?.repo && typeof project.repo === 'object' ? project.repo : {}
      return {
        name: String(repo.full_name ?? ''),
        url: safeRepoUrl(repo.url),
        // null licence is GitHub saying it found none; say so, never blank.
        license: repo.license ? String(repo.license) : 'no licence stated',
        stars: Number.isFinite(Number(repo.stars)) ? Number(repo.stars) : 0,
        facts: Array.isArray(project?.facts) ? project.facts.length : 0,
      }
    })
    .filter((project) => project.name)
  return {
    status,
    text: STATUS_TEXT[status] || (status ? `research ${status}` : 'research status unknown'),
    projects,
    total: Array.isArray(block.projects) ? block.projects.length : 0,
    warnings: (Array.isArray(block.warnings) ? block.warnings : []).map(String).slice(0, 3),
  }
}
