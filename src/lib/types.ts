/** Public v1 contract. JSON Schema plus validateIssue() is authoritative at runtime. */
export type SectionId = 'votes' | 'comments' | 'ask' | 'show';
export interface Availability { status: 'reviewed' | 'unavailable' | 'no-comments' | 'text-post'; note: string; }
export interface Item {
  id: string;
  title: string;
  kind: 'story' | 'ask' | 'show';
  submittedAt: string;
  score: number;
  comments: number;
  sourceUrl?: string;
  discussionUrl: string;
  story: string;
  discussion: string;
  sources: { kind: 'story' | 'discussion'; url: string }[];
  availability: { story: Availability; discussion: Availability };
}
export interface Issue {
  schemaVersion: 1;
  issueId: string;
  revision: number;
  scheduledAt: string;
  window: { start: string; end: string };
  collectedAt: string;
  displayTimezone: 'Europe/London';
  sections: { id: SectionId; itemIds: string[]; qualifyingCount: number }[];
  items: Record<string, Item>;
  correction?: { previousRevision: number; correctedAt: string; note: string };
}
