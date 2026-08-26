# Deep Audit Prompt — Video Review & Approval Workflow

Act as a **senior product designer, UX researcher, video editor, content creator, creative director, and agency operator**.

I want you to thoroughly audit the **video review, feedback, revision, and approval workflow** of this app.

The goal is to create one of the **best, most thoughtfully designed video review experiences** for:

* individual content creators
* freelance video editors
* creators working with dedicated editors
* agencies managing multiple editors
* agencies managing multiple clients
* brands reviewing content from agencies
* internal creative teams
* social media teams
* marketing teams
* clients who are not technically experienced

Do not evaluate the product only as a feature checklist.

Actually imagine yourself using the app repeatedly over weeks and months while working on real videos, deadlines, clients, editors, and multiple revision rounds.

---

# Core Scenarios to Simulate

## Scenario 1 — Creator + Editor

You are a content creator working with an editor.

The workflow might look like:

```text
Creator records footage
↓
Editor creates Version 1
↓
Editor sends video for review
↓
Creator watches it
↓
Creator leaves feedback
↓
Editor receives feedback
↓
Editor makes changes
↓
Editor uploads Version 2
↓
Creator reviews Version 2
↓
More feedback may be added
↓
Version 3
↓
Final approval
↓
Video is ready for publishing
```

Sometimes this process may involve:

```text
V1
V2
V3
V4
V5
```

I want the app to make this process feel effortless.

---

# Scenario 2 — Freelancer + Client

You are a freelance editor working with several clients.

Example:

```text
Client A
  Video 1 — awaiting review
  Video 2 — changes requested
  Video 3 — approved

Client B
  Video 1 — editing
  Video 2 — awaiting client response
```

The editor needs to clearly understand:

* what needs attention
* which videos have feedback
* which comments are unresolved
* which client has responded
* which revision is current
* what has been approved
* what needs to be delivered next

---

# Scenario 3 — Agency + Multiple Clients

Imagine an agency with:

```text
10 clients
20 editors
5 account managers
3 creative directors
100+ videos per month
```

A video may move through:

```text
Editor
↓
Senior Editor
↓
Creative Director
↓
Account Manager
↓
Client
↓
Revision
↓
Internal Review
↓
Client Approval
```

Audit whether the product can scale gracefully from a simple creator/editor relationship to this type of workflow.

---

# Scenario 4 — Multiple Reviewers

Imagine a video being reviewed by:

* the creator
* brand manager
* marketing manager
* founder
* agency
* legal team
* editor

How should feedback work when several people leave comments?

Consider:

* conflicting feedback
* duplicate comments
* comment priority
* reviewer identity
* internal comments
* client-visible comments
* resolved comments
* approval authority

---

# Scenario 5 — Creator Reviewing on Mobile

Imagine the creator receives a notification while away from their computer.

They open the video on their phone and want to:

* watch it
* pause
* leave a comment
* point at something visually
* approve it
* request changes
* reply to the editor
* compare versions

Determine whether the workflow remains excellent on mobile.

---

# Your Task

Go through the complete workflow **step by step**.

Do not stop at the obvious features.

Look for:

* missing states
* awkward transitions
* unnecessary clicks
* confusing terminology
* edge cases
* collaboration problems
* notification problems
* version-control problems
* permission problems
* client-management problems
* review fatigue
* problems that only appear after using the app frequently
* small UX details that would make the experience significantly better

Ask yourself constantly:

> "What would annoy me if I had to use this every day?"

and:

> "What would make me never want to go back to reviewing videos through WhatsApp, Slack, email, Google Drive, Dropbox, Frame.io, or random messages?"

---

# Areas You Must Audit

## 1. Sending a Video for Review

Analyze the ideal workflow for:

```text
Upload video
↓
Choose reviewers
↓
Add message/context
↓
Set review deadline
↓
Send for review
```

Should editors be able to specify:

* what changed
* what feedback they specifically want
* deadline
* priority
* video purpose
* platform
* intended audience
* references
* script
* caption
* thumbnail
* publishing date

Determine what is useful versus unnecessary.

---

# 2. Review Experience

Think deeply about what happens when someone opens a review.

Should they immediately see:

```text
Video
Current version
Who uploaded it
Status
Due date
Outstanding comments
Previous version
```

Determine the best information hierarchy.

The experience should remain clean rather than becoming a project-management dashboard around the video.

---

# 3. Timeline Feedback

Audit commenting deeply.

Consider:

* timestamp comments
* frame-specific comments
* commenting on a time range
* drawing directly on a frame
* arrows
* circles
* boxes
* freehand drawing
* text annotations
* emoji reactions
* voice comments
* video comments
* screen recordings
* attachments
* reference images
* links
* screenshots

Example:

```text
00:07

"This zoom happens too quickly."
```

Or:

```text
00:11–00:14

"Can we replace this entire section with the product demo?"
```

How should these comments appear on the timeline?

---

# 4. Feedback Categories

Would categorizing feedback help?

Examples:

```text
Editing
Audio
Music
Color
Text
Captions
B-roll
Animation
Pacing
Hook
CTA
Brand
Legal
Other
```

Could categories become visual markers on the timeline?

Should they be automatic, manual, or AI-classified?

Avoid unnecessary complexity.

---

# 5. Comment Status

Consider states such as:

```text
Open
Acknowledged
In progress
Resolved
Won't change
Needs clarification
```

Determine whether all of these are necessary.

Think about editor workflows.

An editor should be able to work through feedback almost like a revision checklist.

Example:

```text
12 comments

✓ 8 resolved
◉ 2 in progress
? 2 need clarification
```

---

# 6. Revision Workflow

This is extremely important.

Audit:

```text
Version 1
↓
Feedback
↓
Version 2
↓
Feedback
↓
Version 3
```

When V2 is uploaded:

What happens to V1's comments?

Can users:

* see which changes were addressed?
* automatically mark feedback as resolved?
* reopen old feedback?
* carry unresolved comments into the new version?
* see comments that belonged specifically to the old cut?

Design the cleanest possible behavior.

---

# 7. Version Comparison

Explore whether reviewers should be able to compare:

```text
V1 ↔ V2
```

Potential methods:

* side-by-side playback
* synced playback
* swipe comparison
* quick toggle
* overlay comparison
* difference indicators

Ask whether these genuinely improve reviewing or add unnecessary complexity.

---

# 8. "What Changed?" Experience

Uploading V3 should not force reviewers to watch the entire video again unnecessarily.

Explore features such as:

```text
What's New in V3

00:04 — Hook changed
00:17 — Caption corrected
00:32 — B-roll replaced
00:48 — Music lowered
```

Could the editor manually identify changes?

Could AI detect them automatically?

Could reviewers jump directly between changed sections?

This could significantly reduce review time.

---

# 9. Approval

Define exactly what "approved" means.

Possible states:

```text
Draft
Internal Review
Client Review
Changes Requested
Approved
Final
Published
```

Determine the simplest useful model.

Also consider:

* approving individual comments
* approving sections
* approving the entire video
* approving with minor changes
* multiple required approvers
* final decision maker

---

# 10. Conflicting Feedback

Example:

```text
Client:
"Make the intro shorter."

Creative Director:
"Let the intro breathe longer."

Founder:
"Remove the intro entirely."
```

How should the app handle this?

Explore:

* threading
* @mentions
* decision owner
* feedback consolidation
* internal discussion
* final instruction
* AI summarization

---

# 11. Internal vs External Comments

Agencies often need conversations clients should not see.

Example:

```text
INTERNAL

Editor:
"The client asked for this animation but I think it looks worse."

Creative Director:
"Keep the original and explain why."
```

Then externally:

```text
CLIENT

"We recommend keeping the original animation because..."
```

Design this carefully.

Accidentally exposing internal comments would be a serious product failure.

---

# 12. Notifications

Audit notifications aggressively.

Bad review tools can become notification machines.

Determine when users should receive:

* upload notifications
* review requests
* new comments
* replies
* mentions
* resolution updates
* new versions
* approvals
* deadline reminders

Consider grouping notifications such as:

```text
Sarah left 7 comments on Summer Campaign V2
```

rather than sending seven notifications.

---

# 13. Feedback Summaries

Imagine an editor receives 47 comments.

The app could generate:

```text
47 comments

Major requested changes:

1. Shorten the opening hook.
2. Replace product shot at 00:16.
3. Reduce background music throughout.
4. Correct captions at 00:42.
5. Strengthen CTA.

Conflicting feedback:
2 items

Questions requiring clarification:
3 items
```

Determine where AI can meaningfully reduce cognitive load.

---

# 14. AI Review Assistant

Think carefully about useful AI features.

Not AI for the sake of AI.

Potential features:

### Summarize Feedback

```text
"Summarize everything I need to change."
```

### Detect Conflicting Feedback

```text
Sarah wants the hook shorter.
Alex wants the hook longer.
```

### Turn Feedback Into Tasks

```text
[ ] Shorten intro
[ ] Replace shot
[ ] Correct caption
[ ] Lower music
```

### Compare Versions

AI could identify what changed between V2 and V3.

### Check Whether Feedback Was Addressed

Example:

```text
Comment:
"Move logo slightly higher."

V3:
AI detects logo position changed.

Likely resolved.
```

### Draft Revision Notes

When uploading:

```text
Changes in V4

• Shortened hook
• Replaced B-roll
• Corrected subtitles
• Lowered music
```

Explore which AI features would provide real value.

---

# 15. Editor Revision Mode

Think about whether editors should have a dedicated workspace showing:

```text
Video

REVISIONS

[ ] 00:05 — Remove pause
[ ] 00:12 — Replace B-roll
[ ] 00:27 — Correct caption
[ ] 00:41 — Lower music
```

As they work:

```text
✓ Completed
```

Could this become a powerful editing companion?

Could integrations eventually exist for:

* Premiere Pro
* Final Cut Pro
* DaVinci Resolve
* CapCut
* After Effects

Do not make integrations an MVP requirement unless they materially improve the product.

---

# 16. Client Experience

A client's experience should be dramatically simpler than an editor's.

Imagine receiving:

```text
Your video is ready for review.

[Review Video]
```

They should not have to understand:

* projects
* workspaces
* asset management
* editing terminology
* complex permissions

Determine the ideal zero-friction client review experience.

Could clients review without creating an account?

What are the advantages and risks?

---

# 17. Review Links

Explore the ideal shared-link system.

Consider:

```text
Public link
Password-protected link
Email-restricted link
Expiring link
Client portal
Workspace member
```

Also consider:

* downloading disabled
* watermarked review videos
* link expiration
* viewer tracking
* access revocation

---

# 18. Video Context

Feedback often makes more sense when reviewers know:

```text
Platform: Instagram Reels
Duration goal: <60 seconds
Audience: SaaS founders
Objective: product awareness
CTA: Start free trial
```

Would lightweight creative context improve the quality of reviews?

Could it sit beside the video without cluttering the interface?

---

# 19. Supporting Assets

A video rarely exists alone.

Potential related assets include:

```text
Script
Caption
Thumbnail
Music
Raw footage
Brand guidelines
Reference video
Product assets
Voiceover
Subtitle file
```

Determine which assets belong inside the review experience versus elsewhere.

---

# 20. Review History

Users may later ask:

> "Why did we change this?"

Could the app preserve a decision history such as:

```text
V1
Hook: 8 seconds

Client requested shorter intro.

V2
Hook: 5 seconds

Creative director approved.
```

Explore how useful this is without turning the app into enterprise bureaucracy.

---

# 21. Multiple Videos

Content creators may send:

```text
10 Reels
```

for review at once.

Audit batch workflows:

* review queue
* next video
* keyboard shortcuts
* bulk approve
* batch comments
* project-level comments
* campaign-level feedback

---

# 22. Deadlines

Example:

```text
Client review due:
Today, 5 PM

Publishing:
Tomorrow, 10 AM
```

Could the system intelligently show urgency?

Avoid becoming another project-management product.

---

# 23. Review Inbox

Explore whether each user needs a review inbox.

Example:

```text
Needs Your Attention

Summer Campaign V3
12 comments addressed
Review requested
Due today

Product Launch V2
4 questions waiting for you

Founder Reel #19
Approved
```

This could potentially become one of the core product experiences.

---

# 24. Activity Feed

Determine whether users need an activity history:

```text
Alex uploaded V3
Sarah resolved 4 comments
James requested changes
Alex replied to Sarah
Client approved V3
```

Where is this useful?

Where would it become noise?

---

# 25. Search

Imagine working with hundreds of videos.

Could someone search:

```text
"logo animation feedback"

"videos John approved"

"caption corrections"

"Summer Campaign"
```

Determine what level of search is necessary.

---

# 26. Permissions

Think deeply about permissions for:

```text
Owner
Admin
Creative Director
Editor
Reviewer
Client
Guest
```

Determine whether that is too complex.

Find the smallest permission system that can still support agencies properly.

---

# 27. Agencies

Stress-test the platform for agencies.

Think about:

```text
Agency
├── Client A
│   ├── Campaign 1
│   └── Campaign 2
├── Client B
└── Client C
```

Should agencies have:

* branded client portals
* custom domains
* white labeling
* client-specific workspaces
* reviewer templates
* default approval flows
* client notification preferences

Identify what creates meaningful value versus feature bloat.

---

# 28. Creators

Also make sure we don't overbuild for agencies and make the app unpleasant for a solo creator.

A creator working with one editor should be able to start with something as simple as:

```text
Upload
↓
Send
↓
Comment
↓
Revise
↓
Approve
```

The complexity should appear progressively only when needed.

---

# 29. Keyboard Shortcuts

For people reviewing large numbers of videos, consider:

```text
Space → Play/Pause

C → Comment

A → Approve

J/K/L → Playback

← → Previous frame

→ → Next frame
```

Determine which shortcuts would materially speed up review.

---

# 30. Playback Experience

Audit:

* frame-by-frame playback
* speed controls
* looping sections
* scrubbing
* waveform
* caption visibility
* audio controls
* fullscreen
* theater mode
* mobile playback
* slow motion

Remember that the app is fundamentally a **video review product**.

Playback quality matters enormously.

---

# 31. Offline / Weak Connections

What happens when:

* the reviewer has poor internet
* the video is 4K
* someone opens the review on mobile data
* the upload fails
* the browser closes during upload

Explore:

* adaptive streaming
* upload resume
* processing states
* proxy generation
* error recovery

---

# 32. Edge Cases

Think deliberately about unusual situations.

Examples:

### Editor uploads wrong version.

### Client approves V2 while V3 already exists.

### Reviewer comments on an outdated version.

### Video is replaced while someone is reviewing it.

### Two people reply simultaneously.

### Client changes their mind after approval.

### Editor deletes a version accidentally.

### Reviewer shares the review link externally.

### 100+ comments exist.

### A comment refers to a frame that changed dramatically in V2.

### One reviewer approves while another requests changes.

### Final video differs from the approved version.

For every major edge case, explain how the product should behave.

---

# 33. Competitive Mental Model

Consider why teams currently use combinations of:

* Frame.io
* Vimeo Review
* Dropbox Replay
* Wipster
* Filestage
* Ziflow
* Slack
* WhatsApp
* Google Drive
* Dropbox
* email
* Notion

Do not simply copy their feature lists.

Identify where reviewing video is still unnecessarily frustrating.

Ask:

> What could this product do that makes users think, "I never want to review videos through messages again"?

---

# 34. Micro-Interactions

Pay particular attention to small details.

For example:

When the reviewer pauses and begins typing, automatically attach the current timestamp.

When they draw on the video, pause playback automatically.

When they click a comment, jump to the relevant frame.

When the video passes a comment marker, subtly surface the comment.

When the editor uploads V2, preserve unresolved feedback.

When all requested changes are resolved, surface:

```text
All feedback addressed.

Send V2 for review?
```

Find dozens of opportunities like these.

These small details may become the difference between a decent review tool and an exceptional one.

---

# 35. Emotional Experience

Think beyond functionality.

Different users should feel:

### Client

> "Reviewing this is ridiculously easy."

### Creator

> "I finally know exactly where every video stands."

### Editor

> "I never have to hunt through WhatsApp messages for revision notes again."

### Agency

> "We can handle far more clients without losing track of feedback."

The workflow should create confidence and reduce mental overhead.

---

# Deliverables

After completing the audit, give me:

## 1. Existing Workflow Assessment

Explain what's already strong.

---

## 2. Missing Features

Identify every meaningful gap you can find.

Separate them into:

```text
Critical
High-value
Nice-to-have
Future
Avoid / unnecessary
```

---

## 3. Workflow Problems

Identify places where users may:

* get confused
* lose feedback
* review the wrong version
* accidentally expose information
* receive too many notifications
* repeat work
* wait unnecessarily
* struggle to understand status

---

## 4. Ideal Creator ↔ Editor Workflow

Design the complete flow from:

```text
First upload
→ feedback
→ revisions
→ final approval
```

---

## 5. Ideal Agency ↔ Client Workflow

Design the corresponding scalable flow.

---

## 6. Versioning System

Define exactly how:

```text
V1
V2
V3
V4
```

should behave.

---

## 7. Comment System

Define:

* commenting
* annotations
* threads
* statuses
* internal comments
* client comments
* mentions
* attachments
* comment migration between versions

---

## 8. Approval System

Define the ideal approval states and rules.

---

## 9. Notification System

Specify what gets sent:

* immediately
* grouped
* daily
* only when action is required

---

## 10. AI Opportunities

Recommend only AI features that create substantial workflow improvements.

Rank them by usefulness.

---

## 11. Review Screen

Describe the ideal interface and information hierarchy.

For example:

```text
Header
Video Player
Timeline
Comment Markers
Comment Panel
Version Selector
Review Status
Actions
```

Explain what should appear where and why.

---

## 12. Editor Revision Screen

Design the best workflow for actually processing reviewer feedback.

---

## 13. Review Inbox

Design a centralized "Needs Your Attention" experience.

---

## 14. Agency Architecture

Explain how:

```text
Organizations
Workspaces
Clients
Projects
Videos
Versions
Reviewers
Editors
```

should relate to one another without unnecessary complexity.

---

## 15. Edge Cases

Create a comprehensive edge-case matrix and recommended behavior.

---

## 16. Micro-UX Improvements

Give me at least **30 small details or interactions** that would make the review experience noticeably better.

Avoid generic suggestions.

---

## 17. Biggest Product Opportunities

Identify the **5–10 features or workflow ideas that could genuinely differentiate this product** rather than simply bring it to parity with competitors.

Explain why each matters.

---

## 18. MVP vs Later

Separate everything into:

### MVP

Needed to create an excellent core experience.

### V1.x

Important improvements once the workflow is validated.

### Pro / Agency

Advanced collaboration and operational features.

### Future

Larger strategic bets and integrations.

---

# Final Product Standard

Be extremely critical.

Do not tell me that the workflow is good simply because the basic functionality exists.

Pretend that thousands of:

* creators
* editors
* agencies
* creative directors
* brands

will use this every day.

Look for friction at the level of:

```text
"What happens after I click this?"

"What happens if there are 37 comments?"

"What happens when V3 replaces V2?"

"What does my client see?"

"What does my editor see?"

"Who needs to respond next?"

"How do I know everything was fixed?"

"How do I know which version was actually approved?"

"What happens six months later when someone asks why a change was made?"
```

The goal isn't to create another tool that **allows people to comment on videos**.

The goal is to design the most polished possible system for:

> **Video → Review → Feedback → Revision → Decision → Approval**

It should feel exceptionally simple for a creator working with one editor while being powerful enough to support a professional agency handling dozens of clients.

Prioritize **clarity, speed, confidence, attention to detail, and reduced communication overhead** over feature quantity.
