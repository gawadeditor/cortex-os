---
name: frontend-polish
description: "Apply final quality pass to Factory OS dashboard HTML/CSS: fix spacing inconsistencies, font scale violations, hover states, and visual noise. Use after layout is implemented and before submitting for review. Triggers on: polish, final pass, cleanup, CSS cleanup, spacing fix, typography, hover states, visual consistency, dashboard polish, frontend cleanup, UI cleanup."
---

# Frontend Polish

## CONTEXT

This skill is applied as a final pass before any dashboard implementation is submitted for review. It does not add features. It does not restructure layout. It refines what is already there.

Factory OS uses a hand-written CSS system defined in `_base_style.html`. All polish changes must work within that system. No external CSS frameworks. No utility-class sprawl. Additions to `_base_style.html` must be minimal and purposeful.

Good polish is mostly subtraction: removing inconsistencies, fixing spacing violations, ensuring font sizes follow a clear scale, and making interactive states feel deliberate.

## Responsibilities

- **Spacing system**: Enforce consistent use of `--gap` (16px) as the base unit. Derived spacings: `calc(var(--gap) * 0.5)` (8px), `calc(var(--gap) * 0.75)` (12px), `calc(var(--gap) * 1.5)` (24px), `calc(var(--gap) * 2)` (32px). No magic pixel values outside this system.

- **Typography system**: Enforce the font size scale. Permitted sizes: 11px (labels/meta), 12px (table content/hints), 13px (body/table), 14px (default body), 15px (nav/card title), 20px (page h1), 22–24px (KPI values). Nothing outside this scale. Font weights: 400 (body), 500 (emphasis), 600 (headings/labels), 700 (KPI values/brand).

- **Consistency audit**: Identify cases where the same pattern is implemented with different markup or styles on different pages. Consolidate to a single implementation.

- **Visual refinement**: Check that borders, border-radii, and padding are consistent. `var(--radius)` (6px) is the standard border radius. All cards use `var(--border)` as border color. No exceptions without documented reason.

- **Component quality**: Ensure each shared component (badge, button, card, table) looks and behaves the same across all pages it appears on. Variations must be intentional and named.

- **Interaction polish**: Confirm all interactive elements (links, buttons, form controls) have visible hover states. Hover states must use the established color variables. No opacity tricks. No JavaScript-required hover effects.

## Procedure

### Pass 1 — Spacing audit

Read each template under review. For every padding, margin, and gap value:
- Check it is expressed as a CSS variable or derived from `--gap`
- Flag any hard-coded pixel value that doesn't match the scale
- Flag any inline style that could be a CSS class

Output: list of spacing violations with template line numbers.

### Pass 2 — Typography audit

For every `font-size` declaration:
- Check it is within the permitted scale
- Check the font-weight is appropriate for the element type
- Check `text-transform: uppercase` is only on labels and badge text
- Check `letter-spacing` is only on labels (`.05em`) and badge text (`.04em`)
- Flag any `line-height` that is not `1.5` (body) or `1.2` (KPI values)

Output: list of typography violations.

### Pass 3 — Consistency matrix

Produce a table of shared components across pages. For each component, list the pages it appears on and check:
- Same HTML structure? Y/N
- Same CSS classes? Y/N  
- Same visual result? Y/N

Flag discrepancies.

### Pass 4 — Interactive state check

For each interactive element:
- Link: confirm color change on hover (blue or text)
- Button: confirm border-color + color change, or background change
- Form control: confirm `border-color: var(--blue)` on focus, no outline
- Table row: confirm `rgba(255,255,255,.02)` background on hover

Flag missing hover/focus states.

### Pass 5 — Inline style cleanup

Identify inline `style=""` attributes that:
- Duplicate an existing CSS class
- Could be replaced with a new utility class
- Are used on more than one element across templates

Propose CSS class additions for each case. Classes added to `_base_style.html` must follow the existing naming convention (kebab-case, semantic).

### Pass 6 — Color variable compliance

Check every color value in templates. All colors must use the defined CSS variables:
- `var(--bg)`, `var(--surface)`, `var(--border)`
- `var(--text)`, `var(--muted)`
- `var(--green)`, `var(--yellow)`, `var(--red)`, `var(--blue)`
- Derived: `#1a3a22` (green bg), `#3a0f0a` (red bg), `#1a2a3a` (blue bg), `#3a2e0d` (yellow bg)

Flag any hex value used directly in templates that is not in the approved set. If it matches a derived value, replace with a new CSS variable.

## Deliverables

1. **Spacing violation list** — file, line, current value, correct value
2. **Typography violation list** — file, line, current value, correct value
3. **Consistency matrix** — components across pages with mismatch flags
4. **Interaction gap list** — missing hover/focus states
5. **Inline style consolidation plan** — proposed class names and `_base_style.html` additions
6. **Color compliance report** — unapproved color values and replacements

## Review Checklist

- [ ] No pixel value outside the spacing scale
- [ ] No font-size outside the permitted scale
- [ ] No inline style that could be a named class
- [ ] Every button has a hover state
- [ ] Every link has a hover state
- [ ] Every form control has a focus state
- [ ] Badge variants are exhaustive and consistent across pages
- [ ] `_base_style.html` additions follow naming conventions
- [ ] No color hex values in templates except the approved derived set

## Suggested Skills

- `dashboard_ux` — polish follows layout; run this skill after layout is implemented
- `ui_ux_audit` — run audit first; polish fixes the minor-severity findings
