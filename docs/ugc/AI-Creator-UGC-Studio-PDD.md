# PDD — AI Creator & UGC Studio

**Working title:** TBD  
**Document type:** Product Design Document  
**Version:** 2.0  
**Date:** August 2026  
**Product category:** AI Creator Studio / AI UGC / Social Video / Performance Creative

---

# 1. Product Summary

## 1.1 One-line description

A platform for creating persistent, realistic AI creators and using them to generate organic short-form social content, product UGC, talking-head videos, hook + demo ads, text-on-screen videos, and reference-inspired remixes.

## 1.2 Core promise

> **Create your AI creator once. Use them to make social content again and again.**

Users can:

- choose a platform-provided AI creator
- create a custom AI creator from reference images
- preserve the creator's identity across videos
- generate organic content for TikTok, Instagram Reels, and YouTube Shorts
- create product UGC without hiring a real creator
- upload a reference video and recreate its structure with their own AI creator
- add their own product to the recreation
- generate talking-head content
- create hook + demo videos
- create text-on-screen looping videos
- generate social-first scripts, hooks, storyboards, and scenes
- generate multiple variations quickly

The product should feel like an **AI creator production team**, not a collection of AI APIs.

---

# 2. Product Vision

The long-term vision is to become the operating system for AI-native social creators and UGC production.

A user should be able to create an AI person such as:

```text
AVA
AI Lifestyle Creator
```

and then use Ava continuously for:

```text
Organic Content
Product UGC
Talking Heads
Storytime
POV
Hot Takes
Trend Remixes
Hook + Demo
Text on Screen
Product Reviews
Educational Content
Sponsored Posts
```

The AI creator becomes a reusable digital asset with:

- consistent face
- consistent body
- consistent voice
- personality
- content style
- recurring environments
- preferred wardrobe
- niche
- audience
- tone
- visual identity
- social media strategy

The platform should allow one AI creator to power an entire social media account.

---

# 3. Product Positioning

Do not position the product as:

> AI avatar generator

Do not position it as:

> AI UGC ad generator

Do not position it as:

> Veo wrapper

Preferred positioning:

> **AI Creator Studio for Social Media & UGC**

Alternative:

> **Build an AI creator. Turn ideas, products, and reference videos into social content.**

Alternative:

> **Your AI creator. Unlimited content.**

---

# 4. Core Product Thesis

The user should not manage:

- GPT
- Claude
- Gemini
- OpenRouter
- Veo
- Omni
- ElevenLabs
- lip sync
- prompting
- image generation
- scene stitching
- compositing
- caption generation
- rendering

The user should manage:

```text
Creator
Idea
Product
Reference
Creative Direction
```

The system manages everything else.

---

# 5. Main Product Objects

The platform revolves around five major objects:

```text
Creator
Product
Reference
Content
Campaign
```

## Creator

A persistent AI identity.

## Product

A physical or digital product that can appear in UGC.

## Reference

An uploaded or saved social video used for structure/style inspiration.

## Content

A generated video or creative.

## Campaign

A collection of related content, products, references, and variants.

---

# 6. Foundational AI Systems

The platform is built around three intelligence layers.

```text
CREATOR BRAIN
+
PRODUCT BRAIN
+
VIDEO DNA
        ↓
AI CREATIVE DIRECTOR
        ↓
SCRIPT + STORYBOARD
        ↓
SCENE ENGINE
        ↓
GENERATION ROUTER
        ↓
EDITOR
        ↓
FINAL SOCIAL VIDEO
```

---

# 7. Creator Brain

Every AI creator should have a persistent profile.

Example:

```text
AVA

Identity
Female-presenting
Mid-20s appearance
Warm brown skin
Dark wavy shoulder-length hair
Slim build

Personality
Warm
Funny
Slightly sarcastic
Confident

Voice
Casual
Internet-native
Short sentences
Natural pauses

Content Niches
Lifestyle
Beauty
Productivity
Coffee
Relationships

Audience
Women 18–30

Content Mix
40% Lifestyle
20% Beauty
15% Storytime
15% Opinions
10% Sponsored

Camera Style
Phone camera
Handheld
Natural light
Slight framing imperfections

Common Locations
Bedroom
Kitchen
Bathroom
Coffee shop
Car
Street

Wardrobe
Neutral casual
Athleisure
Minimal fashion
```

This profile informs every future generation.

---

# 8. Product Brain

Every product has persistent structured information.

Example:

```text
GlowSkin Vitamin C Serum

Category
Skincare

Price
$39

Audience
Women 20–40

Benefits
Brightening appearance
Lightweight texture
Easy daily use

Pain Points
Dull-looking skin
Heavy products
Complicated routines

Offer
20% off first purchase

Claims
Vegan
Cruelty free

Guardrails
Do not claim:
- cures acne
- medical treatment
- guaranteed results

Assets
7 photos
3 videos
1 logo
```

---

# 9. Video DNA

Every uploaded reference video can be decomposed into a structured representation.

The platform should analyze:

- total duration
- aspect ratio
- shot count
- scene boundaries
- hook type
- camera angle
- framing
- motion
- gestures
- facial expressions
- body movement
- product interaction
- dialogue
- dialogue purpose
- text overlays
- caption placement
- pacing
- transitions
- cuts
- B-roll
- sound
- music
- CTA
- visual rhythm
- scene timing
- environmental setting

Example:

```text
VIDEO DNA

Duration
14.2 seconds

Format
Hook + Demo

SCENE 1
0.0–1.8 sec
Medium selfie
Creator walks into frame
Direct eye contact
Raised eyebrow
Hook intent: contradiction

SCENE 2
1.8–4.2 sec
Product close-up
Hand enters frame
Product rotates clockwise
Hard cut

SCENE 3
4.2–7.5 sec
Over-shoulder demo
Creator applies product
Voiceover continues

SCENE 4
7.5–11.0 sec
Creator reaction
Close-up
Benefit statement

SCENE 5
11.0–14.2 sec
Product + CTA
```

Video DNA becomes reusable.

---

# 10. Target Users

## 10.1 AI Creator Operators

People running realistic AI character accounts on:

- TikTok
- Instagram
- YouTube Shorts
- X video
- other social platforms

They need frequent content without manually generating every clip.

## 10.2 Ecommerce Brands

Need UGC-style ads without constantly sourcing real creators.

## 10.3 Agencies

Need scalable UGC production across many clients.

## 10.4 Founders / SaaS Companies

Need creator-style promotional content and app demos.

## 10.5 Social Media Managers

Need large volumes of short-form content.

## 10.6 AI Influencer Businesses

Need persistent identities and repeatable content generation.

---

# 11. Jobs To Be Done

## Creator Operator

> I want one AI person I can use repeatedly so my account feels like it belongs to the same creator.

## Organic Content

> I want to generate social posts that feel native to TikTok/Reels instead of looking like ads.

## Product UGC

> I want to create believable creator-led product content without paying a real person for every video.

## Reference Remix

> I found a short-form video format I like and want to adapt its structure using my own creator, script, and product.

## Creative Strategy

> I want AI to help me decide what to post and how to structure the content.

---

# 12. Primary Navigation

Suggested navigation:

```text
Home
Creators
Create
Products
References
Content
Campaigns
Assets
Brand
Settings
```

The most prominent action:

```text
+ Create
```

---

# 13. Home Dashboard

Example:

```text
Good morning

AVA
Your AI Creator

[ + Create Content ]

Recent Content

Morning Routine
Organic
Draft

Glow Serum
Product UGC
Ready

3 Things I Stopped Doing
Text on Screen
Published

Saved References
12
```

If the user has multiple creators:

```text
Creators

Ava
Lifestyle

Maya
Beauty

Chris
Tech

[ + New Creator ]
```

---

# 14. Creator Library

Users can select from platform-provided creators.

Example:

```text
Maya
Beauty / Lifestyle

Chris
Tech / Productivity

Sarah
Wellness / Lifestyle

Daniel
Fitness

Sofia
Fashion

Jordan
Founder / Business
```

Filters:

- visual presentation
- age appearance
- niche
- tone
- language
- accent
- energy
- style
- creator archetype

---

# 15. Create Custom Creator

Users can create a custom AI creator.

Entry point:

```text
Creators
↓
+ Create Custom
```

Flow:

```text
Upload reference image(s)
↓
Analyze identity
↓
Generate structured character specification
↓
Create character pack
↓
User approves
↓
Save Creator
```

---

# 16. Custom Creator Input

Allow:

- one photo
- multiple reference photos
- optional full-body reference
- optional wardrobe reference
- optional environment reference

Recommended upload categories:

```text
Face
3/4 Face
Side
Full Body
Optional Outfit
Optional Environment
```

---

# 17. Reference Image Analysis

A multimodal LLM analyzes the source reference.

The model should:

- identify visual characteristics
- describe wardrobe
- describe environment
- describe lighting
- describe camera framing
- describe visual style
- ignore overlays
- ignore captions
- ignore stickers
- ignore UI
- ignore decorative text
- ignore unrelated logos
- ignore platform chrome

Output should be structured JSON.

Example:

```json
{
  "character": {
    "age_appearance": "mid 20s",
    "face_shape": "oval",
    "skin_tone": "medium warm brown",
    "hair": {
      "color": "dark brown",
      "style": "loose waves",
      "length": "shoulder length"
    },
    "eyes": "dark brown",
    "build": "slim",
    "wardrobe": "white fitted t-shirt"
  },
  "environment": {
    "type": "modern apartment bedroom",
    "lighting": "soft natural window light",
    "camera": "smartphone selfie"
  },
  "ignore": {
    "captions": true,
    "text_overlays": true,
    "stickers": true,
    "ui": true
  }
}
```

---

# 18. Critical Identity Rule

The JSON description is **not enough** to preserve identity.

The source image must remain a locked visual reference.

Architecture:

```text
Reference Image
      │
      ├──────────────┐
      ▼              ▼
Visual Identity   LLM Analysis
      │              │
      │              ▼
      │        Character JSON
      │              │
      └───────┬──────┘
              ▼
        Creator Identity
```

Every future generation should use:

- identity reference
- character specification
- approved creator pack

---

# 19. Character Pack Generation

Before the creator is saved, generate a reusable visual pack.

Potential outputs:

- neutral portrait
- smiling portrait
- talking portrait
- surprised reaction
- side profile
- 3/4 view
- full body
- seated
- standing
- indoor
- outdoor
- selfie
- camera-facing

User sees:

```text
Does this look like your creator?

[ Save Creator ]
[ Regenerate ]
[ Adjust ]
```

---

# 20. Creator Identity Lock

Each saved creator should include:

```text
Identity Lock
ON
```

The system should preserve:

- face shape
- hair
- skin tone
- apparent age
- body proportions
- recurring style
- defining visual characteristics

Optional settings:

```text
Hair
Locked / Flexible

Wardrobe
Flexible

Makeup
Flexible

Environment
Flexible

Body
Locked
```

---

# 21. Creator Voice

Each creator can have a persistent voice.

Options:

- default generated voice
- premium AI voice
- ElevenLabs voice
- licensed clone
- user's own voice
- uploaded voiceover

Voice profile:

```text
Tone
Warm

Energy
Medium

Speed
Conversational

Accent
American English

Delivery
Casual
```

---

# 22. Creator Personality

Users may edit the creator's personality.

Suggested controls:

```text
Funny
Serious
Warm
Sarcastic
Confident
Calm
High Energy
Educational
Opinionated
Playful
```

Advanced:

```text
Write personality notes
```

---

# 23. Creator Social Identity

Each creator can have optional social strategy.

Fields:

```text
Niche
Audience
Content Pillars
Posting Style
Topics
Never Discuss
Brand Partnerships
Recurring Series
```

Example:

```text
Content Pillars

40% Lifestyle
25% Beauty
15% Storytime
10% Productivity
10% Sponsored
```

This prevents every generated post from feeling random.

---

# 24. Create Menu

When the user clicks:

```text
+ Create
```

show:

```text
From an Idea
Remix a Video
Product UGC
Talking Head
Hook + Demo
Text on Screen
Storytime
Trend
Product Review
```

The system may expand over time.

---

# 25. Create From an Idea

User enters:

```text
"3 things I stopped doing that made my mornings easier"
```

Then chooses:

```text
Creator
Ava

Platform
TikTok

Length
Auto

Style
Casual
```

The AI Creative Director creates:

- hook
- script
- scene plan
- camera plan
- captions
- CTA if necessary

---

# 26. Organic Social Content

Organic content should be a first-class use case.

Formats:

- storytime
- hot take
- listicle
- POV
- advice
- relatable observation
- morning routine
- GRWM
- mini vlog
- educational
- opinion
- comedy
- motivational
- trend response
- reaction
- storytelling
- personal update
- lifestyle
- aesthetic loop
- text-on-screen
- talking-head

The platform must not make every video feel like an advertisement.

---

# 27. Product UGC

Product UGC combines:

```text
Creator Brain
+
Product Brain
+
Creative Strategy
```

Input:

```text
Select Creator
Select Product
Select Goal
```

Optional:

- platform
- length
- audience
- offer
- tone

Then AI proposes concepts.

---

# 28. Product Input

Support:

- product URL
- product photos
- product videos
- screen recording
- manual description
- logo
- brand assets
- customer testimonials

Product URL should be optional.

---

# 29. Product UGC Formats

Support:

- Hook + Demo
- Talking Head
- Testimonial
- Problem → Solution
- Personal Discovery
- Product Review
- Unboxing
- Before / After
- Storytime
- Comparison
- Founder Style
- SaaS Demo
- App Demo
- Text on Screen

---

# 30. Hook + Demo

Core structure:

```text
Attention Hook
↓
Product Reveal
↓
Demo
↓
Benefit
↓
Proof
↓
CTA
```

Example:

```text
Scene 1
Creator:
"I genuinely thought this was overhyped."

Scene 2
Product close-up

Scene 3
Creator demonstrates product

Scene 4
Reaction

Scene 5
CTA
```

---

# 31. Text on Screen

User chooses:

```text
Creator
Ava

Text
"I stopped doing these 5 things and my mornings got easier"

Background
Auto
```

System generates:

- natural loop
- creator movement
- appropriate background
- social-native typography
- caption-safe positioning
- subtle motion

Options:

```text
Native
Minimal
Bold
Aesthetic
Storytime
```

---

# 32. Talking Head

Input:

```text
Creator
Ava

Topic
"Why checking your email first thing is ruining your morning"

Length
30 sec

Tone
Hot Take
```

AI generates:

- hook
- script
- gestures
- facial expression plan
- camera framing
- creator delivery
- captions
- jump cuts
- final render

---

# 33. Storytime

Structure:

```text
Curiosity Hook
↓
Context
↓
Tension
↓
Escalation
↓
Payoff
↓
Optional CTA
```

Allow:

- personal story
- fictional creator story
- brand story
- product discovery story

---

# 34. Reference Video Remix

This is a core differentiator.

User:

```text
Upload Video
or
Choose Saved Reference
```

Then:

```text
Analyze Video
```

The platform extracts Video DNA.

---

# 35. Reference Analysis

Video analysis should identify:

## Structure

- hook
- body
- CTA
- narrative pattern

## Shot Details

- shot type
- angle
- framing
- distance
- camera movement
- handheld/static
- focal emphasis

## Creator Actions

- walking
- sitting
- pointing
- turning
- holding item
- opening item
- looking away
- eye contact
- reactions
- gestures

## Product Actions

- holding
- rotating
- applying
- opening
- unboxing
- using
- placing
- showing to camera

## Editing

- cuts
- scene duration
- text
- captions
- zooms
- speed
- overlays
- transitions

## Audio

- speech
- pacing
- music
- SFX
- pauses

---

# 36. Reference Remix Output

The user can choose:

```text
Creator
Ava

Product
Glow Serum

Remix Mode
Structure Match
```

System outputs a new storyboard using:

- same general shot progression
- similar timing
- similar energy
- similar content format
- user's creator
- user's product
- new dialogue
- new branding
- adjusted environments where needed

---

# 37. Remix Strength

Suggested UI:

```text
Reference Influence

Structure    ●●●●●
Camera       ●●●●○
Actions      ●●●●●
Timing       ●●●●●
Environment  ●●●○○
Dialogue     ●●○○○
```

Presets:

```text
Inspired
Close Structure
High Fidelity
```

High-fidelity reconstruction should only be enabled for content the user owns or is authorized to reproduce.

---

# 38. Originality & Rights Controls

The platform should distinguish between:

## Owned / Licensed Reference

Allow closer reconstruction.

## Inspiration Reference

Use:

- structure
- pacing
- scene types
- creative grammar

but generate:

- new dialogue
- new expressive details
- new branding
- new scene-specific choices

Avoid literal unauthorized duplication.

---

# 39. Reference Library

Users can save inspiration.

Example:

```text
Saved References

Hook + Demo
13 sec

Talking Head
24 sec

Text on Screen
8 sec

Storytime
31 sec
```

Actions:

```text
Use Format
Analyze
Tag
Add to Collection
Delete
```

---

# 40. Viral Format Library

Later, the platform can convert frequently used reference structures into reusable formats.

Example:

```text
FORMAT #1842

0–2 sec
Visual movement hook

2–5 sec
Contrarian statement

5–9 sec
Product reveal

9–14 sec
Demo

14–18 sec
Benefit

18–21 sec
CTA
```

User clicks:

```text
Use With Ava
```

No reference upload required.

---

# 41. Trend Library — Later

Potential feature:

```text
Trending Formats
Trending Hooks
Trending Editing Styles
Trending Audio Structures
```

The product should not guarantee virality.

Preferred language:

> Designed for short-form social performance.

Avoid:

> Guaranteed viral video.

---

# 42. AI Creative Director

The AI Creative Director should be central across all creation modes.

Responsibilities:

- understand the creator
- understand the product
- understand the reference
- identify audience
- choose hook
- choose structure
- recommend duration
- choose creator behavior
- write natural dialogue
- determine shot list
- determine where real media should be used
- decide where generative media is safe
- design CTA if needed

---

# 43. Creative Concepts

For Product UGC, AI should generate complete concepts.

Example:

```text
PERSONAL DISCOVERY

Creator
Ava

Hook
"I thought this was just another product TikTok was overhyping."

Story
Skeptical → demo → result → recommendation

Length
22 sec

Scenes
Selfie
Product close-up
Demo
Reaction
CTA
```

User actions:

```text
Use This
More Like This
Change Creator
Edit
```

---

# 44. Avoid Excessive Wizard UX

Do not make users step through:

```text
Angle
↓
Hook
↓
Creator
↓
Script
↓
Voice
↓
Model
↓
Scene
```

Instead:

```text
Idea
↓
Concept
↓
Storyboard
↓
Generate
```

Advanced controls should remain optional.

---

# 45. Script System

Scripts must sound native to social media.

Avoid:

> Introducing the revolutionary skincare solution designed to transform your routine.

Prefer:

> Okay, I genuinely thought this was overhyped.

Rules:

- contractions
- conversational language
- short sentences
- pauses
- reactions
- natural hesitation when appropriate
- imperfect but intentional rhythm
- no unnecessary corporate language
- no fake testimonials
- no unsupported claims

---

# 46. Storyboard

Every generated content item should become a scene-based storyboard.

Example:

```text
Scene 1
0–2 sec
Creator selfie
Hook

Scene 2
2–5 sec
Product reveal

Scene 3
5–9 sec
Demo

Scene 4
9–14 sec
Creator reaction

Scene 5
14–18 sec
CTA
```

---

# 47. Scene Types

Possible scene types:

- Creator Talking
- Creator Reaction
- Creator Walking
- Creator + Product
- Product Close-up
- Product Demo
- Upload
- Screen Recording
- Lifestyle B-roll
- AI B-roll
- Review Screenshot
- Text Card
- CTA Card
- Voiceover
- Split Screen
- Looping Visual

---

# 48. Scene-Level Editing

Users should edit individual scenes instead of regenerating whole videos.

Actions:

```text
Regenerate
Change Dialogue
Change Action
Change Camera
Change Expression
Change Creator
Change Outfit
Change Environment
Change Product
Use Uploaded Media
Duplicate
Delete
Move
```

Natural-language instruction:

```text
"Make her look more skeptical."

"Have her walk into frame."

"Use my real product clip here."

"Make this feel more casual."

"Keep the same timing but change the environment."
```

---

# 49. Generation Router

The backend should automatically select the best available model.

Potential tasks:

```text
Creative Strategy
→ GPT / Claude

Character Analysis
→ multimodal LLM

Reference Analysis
→ multimodal video-capable LLM

Creator Generation
→ selected video model

Reference-Based Video
→ Gemini / Omni-type multimodal generation

Lifestyle B-roll
→ Veo / equivalent

Voice
→ native model / ElevenLabs

Captions
→ speech + text layer
```

The exact providers should remain replaceable.

---

# 50. OpenRouter

OpenRouter may be used as an orchestration layer for:

- Claude
- GPT
- Gemini
- multimodal models
- fallback routing

The application should not expose OpenRouter to the user.

---

# 51. Model Abstraction

Default UI:

```text
Quality

Fast
Standard
Premium
```

Advanced UI:

```text
Generation Model
Auto — Recommended
Advanced
```

Users should not need to know which provider produced a scene.

---

# 52. Product Fidelity

For Product UGC, preserve the user's actual product.

Critical details:

- packaging
- labels
- logo
- colors
- shape
- text
- UI
- product dimensions

The platform should not blindly regenerate exact branding.

---

# 53. Product Fidelity Modes

## Locked Product

Use original assets.

Suitable for:

- product beauty shot
- labels
- logos
- interface screenshots
- packaging

## Assisted Interaction

Use visual reference for:

- holding
- using
- demonstrating
- opening

## Fully Generative

Use for:

- background
- environment
- generic lifestyle B-roll

---

# 54. Hybrid Media

The preferred output can combine:

```text
AI Creator
+
Real Product Asset
+
Generated B-roll
+
Real Product Demo
+
AI Voice
+
Real Review Screenshot
```

Do not force every pixel to be generated.

---

# 55. Automatic Editing

After scene generation, automatically add:

- cuts
- captions
- zooms
- text emphasis
- transitions
- music
- sound effects
- silence trimming
- audio normalization
- CTA
- safe-area correction

---

# 56. Editing Styles

Presets:

- TikTok Native
- Reels Native
- Raw UGC
- Clean Minimal
- High Energy
- Beauty
- Lifestyle
- SaaS / Tech
- Storytime
- Direct Response
- Premium
- Gen Z

---

# 57. Authenticity Slider

Optional:

```text
Raw UGC  ─────────────  Polished Ad
```

Raw:

- handheld
- imperfect framing
- simple captions
- minimal effects

Polished:

- cleaner shots
- refined product media
- more controlled transitions
- premium audio

---

# 58. Quality Assurance

The system should automatically inspect:

- identity drift
- face changes
- hair inconsistency
- product mutation
- logo corruption
- text errors
- lip-sync problems
- strange hands
- broken interactions
- scene discontinuity
- audio clipping
- caption overflow
- repeated frames
- awkward cuts

---

# 59. Generation Failure UX

Do not fail the entire project.

Example:

```text
Scene 3 needs attention.

The product label changed during generation.

Recommended:
Use your uploaded product footage.

[ Use Original Asset ]
[ Try Again ]
[ Replace Scene ]
```

---

# 60. Content Variations

After a content item is approved:

```text
Create Variations
```

Possible variations:

- hooks
- opening shots
- scripts
- CTA
- creator
- environment
- caption style
- length
- platform
- audience
- product angle

---

# 61. Smart Scene Reuse

Example:

```text
Master

Scene 1 Hook
Scene 2 Product
Scene 3 Demo
Scene 4 Benefit
Scene 5 CTA
```

Generate 5 hook variants by replacing only Scene 1.

Benefits:

- faster generation
- lower costs
- greater consistency

---

# 62. Creator Consistency Across Variations

When generating variants, preserve:

- creator identity
- voice
- personality
- recurring visual traits

Allow controlled variation in:

- outfit
- location
- camera
- expression
- energy

---

# 63. Content Calendar — Later

Users running AI creator accounts may need:

```text
Monday
Hot Take

Tuesday
Lifestyle

Wednesday
Storytime

Thursday
Product UGC

Friday
Trend Remix
```

AI can suggest a balanced content plan based on Creator Brain.

---

# 64. Social Strategy — Later

Creator strategy could include:

- content pillars
- hook styles
- recurring series
- post frequency
- audience interests
- creator voice
- sponsored/organic balance

This is especially important for persistent AI influencer accounts.

---

# 65. Performance Feedback — Later

Users may import:

- views
- watch time
- 3-second hold
- completion rate
- likes
- shares
- comments
- saves
- CTR
- CPA
- ROAS

AI can learn:

```text
This creator's hot-take videos outperform listicles.

Product-first openings underperform.

9–14 sec videos have the highest completion rate.

Bathroom UGC outperforms bedroom UGC.

Question hooks underperform direct statements.
```

---

# 66. Creator Memory

Over time:

```text
AVA PERFORMANCE MEMORY

Best Formats
Storytime
Talking Head
Hook + Demo

Best Hook Style
Contrarian

Best Length
14–22 sec

Best Environment
Kitchen

Best Topics
Lifestyle
Relationships
Beauty

Weak Formats
Generic listicles
```

This informs future generations.

---

# 67. Campaigns

For advertisers:

```text
Campaign
Summer Launch

Product
Glow Serum

Creator
Ava

References
4

Master Ads
3

Variations
14
```

---

# 68. Assets

All user assets should remain reusable.

Categories:

- Creator References
- Product Photos
- Product Videos
- Screen Recordings
- Logos
- Reviews
- Generated Scenes
- Voiceovers
- Backgrounds
- Music
- Captions
- Exports

---

# 69. Quick Create Mode

For normal users:

```text
Choose Creator
↓
Choose What to Make
↓
Describe Idea / Add Product / Upload Reference
↓
Generate
```

This should be the default.

---

# 70. Studio Mode

For advanced users:

- script editing
- storyboard
- scene-level control
- reference influence
- creator controls
- product controls
- voice
- generation quality
- model overrides
- timeline
- variants

---

# 71. Timeline Editor — Later / Advanced

The default editor is storyboard-based.

Advanced timeline can support:

- trim
- split
- reorder
- transitions
- music
- audio
- captions
- overlays
- B-roll
- scene timing

Do not require timeline editing for normal creation.

---

# 72. Mobile Experience

The product should eventually support mobile-friendly review and creation.

Mobile flow:

```text
Select Creator
↓
Choose Content Type
↓
Add Idea / Product / Reference
↓
Generate
↓
Preview
↓
Regenerate Scene
↓
Export
```

Desktop remains preferable for advanced editing.

---

# 73. Export

Formats:

- 9:16
- 1:1
- 4:5
- 16:9

Outputs:

- MP4
- clean video
- captioned video
- SRT
- VTT
- individual scenes
- thumbnail

---

# 74. Rights & Consent

Critical policies:

Users must confirm rights for:

- uploaded real-person references
- voice cloning
- custom likeness generation
- third-party creator footage
- customer footage
- testimonials
- product assets
- licensed reference videos

Custom real-person creators should require consent.

The system should avoid unauthorized impersonation.

---

# 75. Reference Rights

For reference video recreation:

## Owned Content

Allow high-fidelity reconstruction.

## Licensed Content

Allow within licensed terms.

## Third-Party Inspiration

Default to:

- structural inspiration
- timing inspiration
- format adaptation
- new dialogue
- new visual details

Avoid literal scene-for-scene duplication of distinctive protected expression where the user lacks rights.

---

# 76. Safety / Claims

For product UGC:

Do not invent:

- medical claims
- financial claims
- unsupported outcomes
- fake testimonials
- fake customer quotes
- false product features

The Product Brain should retain guardrails.

---

# 77. Monetization

Possible plans:

## Creator

For solo AI creator accounts.

Includes:

- 1–3 creators
- organic content
- talking heads
- reference remixing
- standard generation

## Pro

For creators / marketers.

Includes:

- more creators
- product UGC
- premium generation
- advanced references
- variations
- premium voices

## Agency

Includes:

- brands
- clients
- campaigns
- multiple workspaces
- team members
- batch generation
- API
- analytics

---

# 78. Credit Model

Credits are mainly consumed by:

- video generation
- premium video models
- premium voice
- image generation
- high-resolution rendering

Try to make these feel inexpensive or effectively unlimited:

- strategy
- scripts
- character descriptions
- Product Brain
- Video DNA
- captions
- storyboard edits

---

# 79. Core Metrics

## Activation

- creator created
- first content generated
- first export
- first reference analyzed

## Creator Retention

- content per creator per week
- returning creators
- creator reuse rate

## Generation Quality

- accepted scenes
- regenerations per scene
- identity drift rate
- product fidelity failure rate

## Content Output

- exported videos per active user
- variations generated
- organic vs UGC mix

## Reference Feature

- references uploaded
- references reused
- remix completion rate

---

# 80. North Star Metric

Early:

> **Export-ready social videos produced per active creator per month.**

Later:

> **High-performing social creatives generated per active creator.**

---

# 81. MVP

The MVP should prove:

> Can a user create a persistent AI creator and repeatedly make believable short-form content with that identity?

## MVP Scope

### Creator

- built-in creator library
- custom creator from photo
- multimodal character analysis
- structured creator spec
- locked visual reference
- creator profile
- creator voice

### Content Types

- From an Idea
- Talking Head
- Product UGC
- Hook + Demo
- Text on Screen
- Reference Remix

### Product

- URL
- photo
- video
- manual description
- Product Brain

### Reference

- video upload
- Video DNA
- storyboard extraction
- creator substitution
- product substitution
- structure-based remix

### Storyboard

- scene cards
- script
- scene actions
- camera instructions

### Generation

- creator scenes
- basic product scenes
- B-roll
- voice
- captions

### Editing

- auto assembly
- scene regeneration
- dialogue edit
- media replace
- caption style

### Export

- 9:16 MP4

---

# 82. MVP Non-Goals

Do not initially build:

- social publishing
- performance analytics
- trend scraping
- full creator marketplace
- full timeline editor
- team collaboration
- mobile app
- influencer marketplace
- automated posting
- advanced campaign analytics
- complex node workflow
- hundreds of generation controls

---

# 83. Phase 2

Add:

- better identity consistency
- creator packs
- multiple creator outfits
- custom environments
- creator voices
- ElevenLabs
- more UGC templates
- Product Fidelity Engine
- higher-quality reference matching
- creator memory
- reusable references
- viral format library
- more variations
- SaaS/app content flows
- localization

---

# 84. Phase 3

Add:

- content calendar
- social strategy
- analytics
- platform integrations
- performance feedback
- trend discovery
- batch generation
- multi-brand workspaces
- team collaboration
- client approval
- API
- creator licensing

---

# 85. Suggested New User Onboarding

```text
Welcome

What do you want to create?

○ AI Creator Social Content
○ Product UGC
○ Both
```

Then:

```text
Choose a Creator

[ Maya ]
[ Chris ]
[ Sarah ]

or

[ Create Custom ]
```

Then:

```text
Create your first video
```

Do not ask 20 questions during onboarding.

---

# 86. Suggested Create Screen

```text
CREATE WITH AVA

From an Idea
Describe anything

Remix a Video
Upload a reference

Product UGC
Promote a product

Talking Head
Speak directly to your audience

Hook + Demo
Scroll-stopping hook + demonstration

Text on Screen
Looping visual + overlay copy

Storytime
Narrative creator video
```

---

# 87. Suggested Custom Creator Screen

```text
Create Your Creator

Upload 1–5 reference photos

[ Drop Images ]

We recommend:
• clear face
• neutral lighting
• different angles

[ Continue ]
```

Then:

```text
We created Ava

[ Preview Portrait ]
[ Preview Full Body ]
[ Preview Talking ]

Identity
Locked

Voice
Ava Natural

[ Save Creator ]
```

---

# 88. Suggested Reference Remix Screen

```text
Remix a Video

[ Upload Video ]

or

Choose from Saved References
```

After analysis:

```text
We found:

Hook + Demo
14 sec
5 scenes

Creator movement hook
Product reveal
Demo
Benefit
CTA

Use with:
Ava

Product:
Glow Serum

Reference Influence:
Close Structure

[ Create Remix ]
```

---

# 89. Suggested Draft Editor

```text
┌───────────────┬────────────────────────┬────────────────────┐
│ SCENES        │ PREVIEW                │ CONTROLS           │
│               │                        │                    │
│ 1 Hook        │                        │ Scene 2            │
│ 2 Product     │       VIDEO            │ Dialogue           │
│ 3 Demo        │                        │ Action             │
│ 4 Reaction    │                        │ Camera             │
│ 5 CTA         │                        │ Creator            │
│               │                        │ Product            │
└───────────────┴────────────────────────┴────────────────────┘
```

---

# 90. Main UX Principles

## Keep creator persistent

The user should not recreate a person every time.

## Hide model complexity

The user picks outcomes, not AI providers.

## Show value quickly

Get to a preview fast.

## Scene-level control

Fix the bad part, not the whole video.

## Product fidelity

Do not destroy the user's product.

## Social-native output

Content should feel like TikTok/Reels, not generic ads.

## Organic + paid

The product must support both.

---

# 91. Main Product Risks

## Identity Drift

The creator slowly looks different.

Mitigation:

- locked visual references
- creator pack
- identity QA
- consistent generation pipeline

## Product Drift

Packaging changes.

Mitigation:

- locked product assets
- compositing
- fidelity checks
- real footage

## Overly AI-Looking Content

Mitigation:

- natural camera movement
- imperfect framing
- realistic pacing
- real product assets
- scene variety
- less polished defaults

## Generic Scripts

Mitigation:

- Creator Brain
- social-native prompts
- reference formats
- creator personality

## Copycat Content

Mitigation:

- originality controls
- structure remixing
- rights-aware high-fidelity modes

## Expensive Generation

Mitigation:

- scene reuse
- low-cost previews
- selective regeneration
- cached assets

---

# 92. Strongest Differentiators

## Persistent AI Creators

The same creator across months of content.

## Creator Brain

Personality and content identity remain consistent.

## Product Brain

UGC is grounded in real product information.

## Video DNA

Reference videos become editable reusable structures.

## Scene-Level Recreation

Users can reproduce the motion grammar of a reference.

## Hybrid Media

Real + AI assets work together.

## Reference Remix

Transform an existing format into the user's creator and product.

## Organic + Paid Content

Not limited to advertisements.

## Creative Memory

The system learns what works for each creator.

---

# 93. Long-Term Product Flywheel

```text
Create AI Creator
↓
Generate Content
↓
Publish
↓
Collect Performance
↓
Learn Best Hooks
↓
Learn Best Formats
↓
Learn Best Environments
↓
Learn Best Topics
↓
Generate Better Content
↓
Grow Creator
↓
More Product UGC
↓
More Revenue
```

---

# 94. Final Product Experience

The ideal experience should feel this simple:

```text
Create Ava
↓
Choose what Ava should make
↓
Give us an idea, product, or reference
↓
Generate
↓
Fix any scene you dislike
↓
Export
```

Underneath, the platform may use:

```text
GPT
Claude
Gemini
OpenRouter
Veo
Omni
ElevenLabs
Image Models
Video Models
Voice Models
Rendering
Compositing
```

But none of that should define the experience.

---

# 95. Final Product Principle

The product should make an AI creator feel like a real reusable creative asset.

The user should feel like they have:

```text
AI Creator
+
Creative Strategist
+
Scriptwriter
+
Director
+
Camera Operator
+
UGC Creator
+
Video Editor
+
Performance Marketer
```

inside one product.

The experience should still feel like:

```text
Choose Creator
↓
Choose Content Type
↓
Add Idea / Product / Reference
↓
Generate
↓
Refine
↓
Export
```

That simplicity is the product.
