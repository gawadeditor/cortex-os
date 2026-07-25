# CHANNEL_PACKAGE_SPEC.md

# Cortex Channel Package Specification

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the official specification for a Cortex Channel Package.

A Channel Package is the complete definition of a media organization.

It contains everything that makes one channel different from another.

It contains nothing related to Cortex implementation.

---

# Principle

Cortex provides capabilities.

A Channel Package provides identity.

Together they produce a complete media organization.

---

# Definition

A Channel Package is a self-contained collection of configuration, knowledge, policies, objectives, and learning that describes a single media organization.

The package is loaded at runtime.

It is never permanently merged into Cortex.

---

# Responsibilities

A Channel Package is responsible for:

- Identity
- Mission
- Audience
- Editorial Standards
- Brand Guidelines
- Production Preferences
- Platform Configuration
- Publishing Strategy
- Learning
- Objectives

Everything else belongs to Cortex.

---

# Package Structure

A Channel Package contains the following logical sections.

---

## Identity

Defines who the channel is.

Examples

- Name
- Description
- Mission
- Vision
- Values
- Tone of Voice

---

## Audience

Defines who the content is created for.

Examples

- Primary audience
- Secondary audience
- Languages
- Geographic focus
- Age groups
- Viewer expectations

---

## Editorial DNA

Defines what the channel creates.

Examples

- Genres
- Storytelling style
- Narrative principles
- Fact standards
- Citation rules
- Ethical policies

---

## Visual DNA

Defines how the content should look.

Examples

- Cinematic style
- Lighting
- Camera language
- Composition
- Motion language
- Color philosophy
- Visual references

---

## Audio DNA

Defines how the content should sound.

Examples

- Narration style
- Music philosophy
- Sound design
- Mixing standards
- Silence usage

---

## Thumbnail DNA

Defines thumbnail strategy.

Examples

- Composition
- Typography
- Color usage
- Emotional triggers
- Curiosity principles
- Branding rules

---

## Prompt DNA

Defines prompting standards.

Examples

- Prompt style
- Prompt constraints
- Prompt examples
- Prompt vocabulary
- Prompt priorities

---

## Platform Strategy

Defines how each platform is used.

Examples

YouTube

TikTok

Instagram

Facebook

Future platforms

Each platform may have independent strategies.

---

## Publishing Strategy

Defines publishing behavior.

Examples

- Publishing schedule
- Weekly targets
- Premiere rules
- Shorts strategy
- Seasonal adjustments

---

## Objectives

Defines measurable success.

Examples

- CTR goals
- Retention goals
- Watch time goals
- Growth goals
- Revenue goals

---

## Policies

Defines operational rules.

Examples

- Budget limits
- Provider preferences
- Safety rules
- Approval policies
- Retry limits

---

## Learning

Stores knowledge specific to this channel.

Examples

- Audience preferences
- Successful topics
- Failed topics
- Thumbnail performance
- Script insights
- Platform observations

Learning never belongs to Cortex Core.

---

## Secrets

Contains credentials required by the channel.

Examples

- OAuth tokens
- API keys
- Platform credentials

Secrets must be isolated.

Secrets are never shared between channels.

---

# Runtime Loading

When execution begins:

1. Cortex starts.

2. Infrastructure loads.

3. Services initialize.

4. Channel Package loads.

5. Execution Context is built.

6. Production begins.

After execution finishes:

The Channel Package remains unchanged except for approved learning updates.

---

# Isolation

Every Channel Package is isolated.

A package cannot:

- Access another package.
- Modify another package.
- Read another package's secrets.
- Modify Cortex.

Isolation is mandatory.

---

# Versioning

Every package has an independent version.

Updating a package must not require updating Cortex.

Updating Cortex must not require modifying existing packages unless compatibility changes.

---

# Validation

Before execution, Cortex validates that a package is complete.

Validation includes:

- Required fields
- Policy consistency
- Secret availability
- Platform configuration
- Learning integrity

Execution must fail if validation fails.

---

# Portability

A Channel Package should be portable.

Moving a package between Cortex installations should require no code changes.

Only environment-specific secrets may differ.

---

# Examples

Examples of valid Channel Packages include:

- Hypogeum
- Technology News
- Sports Analysis
- Finance Education
- Children's Learning
- Corporate Communications

The Cortex platform does not distinguish between them.

Only the package changes.

---

# Authority

This specification defines the official contract between Cortex and every Channel Package.

Any package that does not conform to this specification is considered invalid and must not be executed.