import { describe, expect, it } from 'vitest'
import { MAX_PRIOR_ART_PROJECTS, readPriorArt, safeRepoUrl } from './prior-art.js'

const project = (name, extra = {}) => ({
  repo: { full_name: name, url: `https://github.com/${name}`, license: 'Apache-2.0', stars: 42, ...extra },
  facts: [{ field: 'controller' }],
})

describe('readPriorArt', () => {
  it('is null when research was not asked for', () => {
    expect(readPriorArt({})).toBeNull()
    expect(readPriorArt({ prior_art: null })).toBeNull()
  })

  it('lists the top projects with licence, stars and a link', () => {
    const card = readPriorArt({
      prior_art: {
        status: 'found',
        projects: ['a/1', 'b/2', 'c/3', 'd/4'].map((n) => project(n)),
        warnings: [],
      },
    })
    expect(card.projects).toHaveLength(MAX_PRIOR_ART_PROJECTS)
    expect(card.total).toBe(4)
    expect(card.projects[0]).toEqual({
      name: 'a/1',
      url: 'https://github.com/a/1',
      license: 'Apache-2.0',
      stars: 42,
      facts: 1,
    })
  })

  it('says a missing licence in words and never links off github.com', () => {
    const card = readPriorArt({
      prior_art: { status: 'found', projects: [project('x/y', { license: null, url: 'javascript:alert(1)' })] },
    })
    expect(card.projects[0].license).toBe('no licence stated')
    expect(card.projects[0].url).toBe('')
    expect(safeRepoUrl('http://github.com/a/b')).toBe('')
    expect(safeRepoUrl('https://evil.test/a')).toBe('')
  })

  it('an empty search reads as its status, not as zero projects found', () => {
    const card = readPriorArt({ prior_art: { status: 'rate_limited', projects: [], warnings: ['limit'] } })
    expect(card.text).toContain('rate-limited')
    expect(card.warnings).toEqual(['limit'])
  })
})
