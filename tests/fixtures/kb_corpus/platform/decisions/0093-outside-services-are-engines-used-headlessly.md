---
id: platform/decisions/0093-outside-services-are-engines-used-headlessly
kind: decision
project: platform
title: "Outside services are engines we use headlessly; operating information and action live in the Keel"
status: current
topics: [keel, operating-plane, github, outside-services]
audience: [team, operator]
depends_on:
  - platform/decisions/0033-the-decision-record-lives-in-the-library-and-the-surface-is-interchangeable
last_reviewed: "2026-10-01"
authors: [steward]
---

## The decision

**Every outside service is used headlessly, through its API, for its engine alone. All operating
information is stored in the Keel, and all operating action happens there.** The operator stated it on
2026-07-17, first about GitHub and then about outside services in general:

> "All operating information stays in our box, and we act on it always in our box, and we use the
> GitHub engine as a useful service (version control, CI, backups all integrated)."

> "we use them headlessly for their engines only."

Operating information means bugs, decisions, review findings, pull-request state and
descriptions. Operating action means reviewing, deciding, merging, closing and triaging. An
outside service may hold a copy that its engine needs, but the copy is not the record, and nobody
works in the service's own interface.

## What the engines are

- **GitHub** provides version control, CI compute and an integrated off-site backup. Pull
  requests are the mechanism its engine merges through. Their review record, state and
  description are memory the Library holds.
- **Gmail** provides email transport. The operator's mail is captured into the EA crew's own archive and
  acted on there; as user-owned data it stays out of the Keel's shared vaults.
- **Slack** has been retired as an operating surface. The operator does not read it, and no new Slack
  surface is added.

A service not listed here takes the same rule when it is adopted.

## How the July wording maps to today

In July, "our box" meant the team site at team.convivy.com, Orient's control database and the
memory store on the mini. Those homes have since moved: the Library holds the Reference Room,
the Chart House and the Archives, and Orient and Deck are surfaces onto it
([the decision record lives in the Library](0033-the-decision-record-lives-in-the-library-and-the-surface-is-interchangeable.md)).
The principle is unchanged by the move. For company operating information, "the box" now means
the Keel; for a person's own data, it means that person's own store on our machines.

## What follows

This is the thesis behind several migrations, each applying it to one service: Notion to the
knowledge base, Slack to the Keel's own surfaces, and GitHub to a headless engine. Two open
steward epics apply it to GitHub. One makes the Library's review record the source the merge
gate reads (issue_01M2F52BXECXGHC1P3PRJ28AV8). The other makes Deck draw a pull request's
state from the Library (issue_01M2K2SS2F6GEXCPG91ESGGNAS).

A design that reads operating state back out of an outside service's interface, or asks a
person to act there, contradicts this decision and needs a ruling of its own.
