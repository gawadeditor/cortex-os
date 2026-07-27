"""
hypogeum/video_dna_validation_suite.py — Video DNA Validation Suite v1 (2026-07-14)

Permanent, reusable repository asset. Does NOT validate one video or one
episode — it validates the Hypogeum Video DNA itself: whether
hypogeum.video_dna produces a consistent cinematic language across many
unrelated archaeological situations.

Every scene in this suite is deliberately topic-agnostic — no episode name,
no specific artifact, no specific civilization, no named location. Each
scene tests exactly one cinematic behavior (a row in the table below), not
a topic. When Video DNA changes (v1 -> v2 -> v3 ...), re-run the SAME suite
unchanged and compare results — see hypogeum.video_dna_validator.

Hard rules for every scene's canonical_prompt (enforced by
hypogeum.video_dna_validator, not just convention):
  - No dialogue, no narration, no subtitles, no logos, no on-screen text.
  - No episode-specific wording (no proper nouns naming a specific artifact,
    civilization, or episode).
  - Approximately one Veo clip in scope (~8 seconds of screen action).
  - Canonical prompts are never edited by hand to fix a bad generation —
    if a category consistently produces poor results, the fix belongs in
    hypogeum/video_dna.py (the DNA itself), not in this file.

Public API:
    get_all_scenes()          -> list[dict]
    get_scene(validation_id)  -> dict
    get_categories()          -> list[str]
    SUITE_VERSION             -> str
"""
from __future__ import annotations

SUITE_VERSION = "v1"

# ---------------------------------------------------------------------------
# Validation scenes — one per required cinematic-behavior category.
# ---------------------------------------------------------------------------

_SCENES: list = [
    {
        "validation_id": "discovery_through_movement",
        "category": "discovery_through_movement",
        "objective": "Prove camera movement alone can create a sense of discovery, without a light change or a reveal cut.",
        "what_is_tested": "Whether the model executes a single continuous forward/lateral move that unveils new space as it travels, rather than cutting or jumping to the reveal.",
        "success_criteria": [
            "One continuous, unbroken camera move for the full clip",
            "New architectural space becomes visible progressively as the camera advances",
            "No cut, no jump, no perspective change mid-shot",
            "Movement speed is slow and deliberate",
        ],
        "failure_patterns": [
            "Camera jumps or cuts partway through",
            "Space is fully visible from the first frame (nothing is discovered)",
            "Fast or jerky movement",
            "Camera moves backward instead of advancing into unseen space",
        ],
        "canonical_prompt": (
            "A narrow ancient stone passage curves gently to the right. The camera "
            "moves slowly forward along the passage, advancing at a walking pace. "
            "As it rounds the curve, a larger chamber becomes visible ahead, its "
            "far wall still in shadow. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A single unbroken forward tracking shot that reveals a previously-hidden chamber purely through the camera's own advance.",
    },
    {
        "validation_id": "discovery_through_light",
        "category": "discovery_through_light",
        "objective": "Prove a changing or newly-appearing light source alone can create a sense of discovery, independent of camera movement.",
        "what_is_tested": "Whether the model animates a believable, motivated light change (not a flicker or strobe) that progressively reveals form.",
        "success_criteria": [
            "Light source visibly grows, shifts, or first appears during the clip",
            "Previously-dark surfaces become legible as the light changes",
            "Single motivated light source, not multiple competing lights",
            "No flicker, strobe, or flash",
        ],
        "failure_patterns": [
            "Light is static/unchanging for the whole clip",
            "Flickering, strobing, or flash-style light change",
            "Multiple unmotivated light sources appear",
            "Scene becomes fully lit instantly rather than progressively",
        ],
        "canonical_prompt": (
            "A dark stone chamber, nearly black. A narrow crack in the ceiling "
            "lets in a thin beam of daylight that slowly widens as unseen clouds "
            "pass outside, gradually illuminating carved wall surfaces that were "
            "invisible at the start of the shot. Camera is static. No people, no "
            "text, no modern objects."
        ),
        "expected_visual_behavior": "A static shot where the sole storytelling device is a slowly widening beam of light revealing carved stone detail.",
    },
    {
        "validation_id": "discovery_through_dust",
        "category": "discovery_through_dust",
        "objective": "Prove suspended dust/particulate motion within a light beam can carry atmosphere and depth without camera or subject motion.",
        "what_is_tested": "Whether the model renders believable slow-drifting particulate matter that reads as volumetric, not as a flat texture or artifact.",
        "success_criteria": [
            "Visible dust motes or fine particulate drifting slowly through a light beam",
            "Particulate motion reads as three-dimensional (parallax/depth), not a 2D overlay",
            "Camera and environment otherwise mostly static",
            "Motion speed is slow and continuous",
        ],
        "failure_patterns": [
            "Dust looks like a flat 2D overlay/texture",
            "Dust moves too fast or in an unnatural swirl",
            "No visible dust at all despite the prompt",
            "Dust obscures the entire frame rather than reading as atmosphere",
        ],
        "canonical_prompt": (
            "A shaft of warm daylight cuts diagonally through a dim underground "
            "room, full of fine dust drifting slowly through the beam. The camera "
            "holds a static wide shot as the dust catches the light. No people, "
            "no text, no modern objects."
        ),
        "expected_visual_behavior": "A static shot where slow volumetric dust drifting through a single light beam is the entire visual event.",
    },
    {
        "validation_id": "discovery_through_shadow",
        "category": "discovery_through_shadow",
        "objective": "Prove shadow movement/recession alone can reveal form, as the inverse of the light-discovery scene.",
        "what_is_tested": "Whether the model animates shadow retreat/shift in a way that progressively reveals a shape rather than an instant unmasking.",
        "success_criteria": [
            "A shadow visibly recedes, shortens, or shifts during the clip",
            "A previously-shadowed shape becomes progressively legible",
            "One consistent, motivated light direction throughout",
            "Slow, continuous shadow movement",
        ],
        "failure_patterns": [
            "Shadow is static the whole clip",
            "Shadow jumps or snaps to a new position",
            "Light direction is inconsistent/contradictory across the shot",
            "The revealed shape is not archaeologically plausible",
        ],
        "canonical_prompt": (
            "A large carved stone relief on a chamber wall, mostly hidden in deep "
            "shadow. As unseen daylight outside shifts, the shadow across the wall "
            "slowly recedes, gradually revealing the carved shapes beneath. Camera "
            "is static. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A static shot where a receding shadow is the sole mechanism revealing a carved relief.",
    },
    {
        "validation_id": "object_reveal",
        "category": "object_reveal",
        "objective": "Prove a single hero object can be revealed clearly and legibly as the primary subject of a shot.",
        "what_is_tested": "Whether the model isolates one object as the unambiguous focal subject, correctly scaled and lit, without competing elements.",
        "success_criteria": [
            "Exactly one object reads as the focal subject",
            "Object is clearly lit and legible by the end of the clip",
            "Background remains subordinate (dark, out of focus, or simple)",
            "No other object competes for attention",
        ],
        "failure_patterns": [
            "Multiple objects compete for focus",
            "Object never becomes clearly legible",
            "Background is as bright or detailed as the subject",
            "Camera loses the object mid-shot",
        ],
        "canonical_prompt": (
            "A weathered bronze artifact sits alone on a flat stone surface in a "
            "dark chamber, catching warm rim light along one edge. The rest of the "
            "room stays in near-black shadow. Camera holds a slow, steady frame on "
            "the object. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A held shot in which one clearly-lit object is the unmistakable single subject against a near-black subordinate background.",
    },
    {
        "validation_id": "human_interaction_with_artifact",
        "category": "human_interaction_with_artifact",
        "objective": "Prove a faceless human figure can physically interact with an artifact without becoming a talking-head or presenter shot.",
        "what_is_tested": "Whether the model keeps the figure faceless/silhouetted and the interaction physical (touching, lifting, examining), never speech-adjacent.",
        "success_criteria": [
            "Human figure is faceless or fully silhouetted throughout",
            "Interaction is a physical action (reaching, touching, brushing dust away)",
            "No mouth movement, no gesture resembling speech",
            "Figure does not face or address the camera",
        ],
        "failure_patterns": [
            "Figure's face becomes visible/legible",
            "Figure appears to speak or gesture as if speaking",
            "Figure faces directly toward camera",
            "Interaction reads as posed/statuesque rather than active",
        ],
        "canonical_prompt": (
            "A faceless human figure, seen from behind and in silhouette, kneels "
            "beside a carved stone artifact and slowly brushes dust from its "
            "surface with one hand, backlit by a single warm light source. No "
            "dialogue, no text, no modern objects."
        ),
        "expected_visual_behavior": "A faceless, silhouetted figure performing one slow physical action on an artifact, never facing or addressing the camera.",
    },
    {
        "validation_id": "slow_environmental_motion",
        "category": "slow_environmental_motion",
        "objective": "Prove the environment itself can carry a shot's motion with a static or near-static camera.",
        "what_is_tested": "Whether environmental motion (not camera, not human, not dust) reads as slow and deliberate rather than static or chaotic.",
        "success_criteria": [
            "A non-dust, non-human environmental element visibly moves (water, cloth, smoke, vegetation)",
            "Camera is static or nearly static",
            "Motion speed is slow and continuous",
            "No cuts within the clip",
        ],
        "failure_patterns": [
            "Nothing visibly moves (looks like a still image)",
            "Environmental motion is fast or erratic",
            "Camera moves significantly, competing with the environmental motion as the subject",
        ],
        "canonical_prompt": (
            "Inside a ruined stone structure, a thin trickle of water drips slowly "
            "from a cracked ceiling into a shallow pool below, rippling gently "
            "outward. Warm light falls across the water's surface. Camera is "
            "static. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A static shot in which slow dripping water and its rippling pool are the sole source of motion.",
    },
    {
        "validation_id": "camera_push_in",
        "category": "camera_push_in",
        "objective": "Prove a slow, deliberate push-in toward a fixed subject stays smooth and controlled for the full clip.",
        "what_is_tested": "Whether the push-in is continuous, single-direction, and does not accelerate, stutter, or overshoot.",
        "success_criteria": [
            "Camera moves continuously closer to the subject for the whole clip",
            "Speed remains slow and roughly constant (no ramp)",
            "Subject stays centered/framed throughout",
            "No stutter, jitter, or direction reversal",
        ],
        "failure_patterns": [
            "Speed ramps up partway through",
            "Camera stutters or jitters",
            "Camera reverses direction (pulls back) mid-shot",
            "Subject drifts out of frame",
        ],
        "canonical_prompt": (
            "A carved stone doorway at the end of a dim corridor, faint warm light "
            "glowing beyond it. The camera performs a slow, continuous push-in "
            "toward the doorway at a constant, deliberate pace for the entire "
            "shot. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A single, constant-speed push-in toward a fixed doorway with no acceleration, stutter, or reversal.",
    },
    {
        "validation_id": "camera_orbit",
        "category": "camera_orbit",
        "objective": "Prove a slow orbital move around a central subject stays smooth and maintains consistent framing.",
        "what_is_tested": "Whether the orbit maintains a consistent radius and subject framing without warping, snapping, or losing the subject.",
        "success_criteria": [
            "Camera visibly arcs/orbits around the subject",
            "Subject remains centered and clearly framed throughout",
            "Orbit speed is slow and constant",
            "No warping, snapping, or perspective break",
        ],
        "failure_patterns": [
            "Orbit is too fast or whip-like",
            "Subject leaves frame or becomes unrecognizable mid-orbit",
            "Perspective warps unnaturally",
            "Camera path is jagged rather than a smooth arc",
        ],
        "canonical_prompt": (
            "A large weathered stone statue fragment stands alone in a ruined "
            "courtyard, lit by low warm sunlight. The camera performs a slow, "
            "smooth orbit partway around the fragment, keeping it centered in "
            "frame throughout. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A smooth, constant-radius partial orbit around a stationary subject that remains centered and recognizable throughout.",
    },
    {
        "validation_id": "vertical_reveal",
        "category": "vertical_reveal",
        "objective": "Prove a vertical camera move (tilt or rise/descend) can reveal scale along the vertical axis.",
        "what_is_tested": "Whether the vertical move is smooth and reveals genuinely new information (height, depth) rather than just reframing the same content.",
        "success_criteria": [
            "Camera moves along a clearly vertical axis (tilt, rise, or descend)",
            "New structural information becomes visible as the move progresses",
            "Movement is slow and continuous",
            "Framing stays coherent (no disorientation)",
        ],
        "failure_patterns": [
            "Movement is not clearly vertical",
            "Nothing new is revealed by the move",
            "Move is fast or whip-like",
            "Horizon/verticals appear to warp unnaturally",
        ],
        "canonical_prompt": (
            "Inside a tall ancient stone tower, the camera slowly tilts upward "
            "from a shadowed floor toward a distant circular opening high above, "
            "where a small disc of daylight is visible, revealing the tower's "
            "full height as it moves. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A slow upward tilt that reveals the full vertical scale of an enclosed space, ending on a distant light source.",
    },
    {
        "validation_id": "scale_reveal",
        "category": "scale_reveal",
        "objective": "Prove the composition can communicate relative scale (small human vs. vast structure) within a single shot.",
        "what_is_tested": "Whether the human figure and the structure are both legible and clearly scaled relative to each other, without the figure becoming a talking-head focal point.",
        "success_criteria": [
            "A single small human silhouette is visible against a much larger structure",
            "Relative scale is unambiguous",
            "Figure is faceless/silhouetted and clearly subordinate to the structure",
            "Structure remains the primary subject",
        ],
        "failure_patterns": [
            "Figure is too large / becomes the dominant subject",
            "Scale relationship is ambiguous or unreadable",
            "Figure's face becomes visible",
            "Structure is not identifiable as ancient/archaeological",
        ],
        "canonical_prompt": (
            "A vast ancient stone archway towers over a single small, faceless "
            "human silhouette standing at its base, emphasizing the archway's "
            "immense scale. Warm low sunlight rakes across the stone. Camera "
            "holds a static wide shot. No dialogue, no text, no modern objects."
        ),
        "expected_visual_behavior": "A static wide shot establishing unambiguous scale contrast between a small silhouetted figure and a monumental structure.",
    },
    {
        "validation_id": "interior_ancient_architecture",
        "category": "interior_ancient_architecture",
        "objective": "Prove the model can render a coherent, physically plausible ancient interior space.",
        "what_is_tested": "Whether the interior reads as one continuous, structurally coherent space with consistent lighting, not a collage of disconnected elements.",
        "success_criteria": [
            "Interior architecture is structurally coherent (walls, ceiling, floor relate logically)",
            "Single consistent light source/direction",
            "Materials read as stone/ancient construction",
            "No anachronistic elements",
        ],
        "failure_patterns": [
            "Architecture is structurally implausible or self-contradictory",
            "Multiple inconsistent light sources/directions",
            "Modern materials or objects appear",
            "Space reads as a flat backdrop rather than a real volume",
        ],
        "canonical_prompt": (
            "The interior of a grand ancient stone hall, rows of carved columns "
            "receding into shadow, a single high window casting a shaft of warm "
            "light across the floor. Camera holds a slow static wide shot. No "
            "people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A structurally coherent, single-light-source ancient interior with columns receding in physically consistent perspective.",
    },
    {
        "validation_id": "exterior_archaeological_ruins",
        "category": "exterior_archaeological_ruins",
        "objective": "Prove the model can render coherent exterior ruins in a natural landscape under natural daylight.",
        "what_is_tested": "Whether exterior ruins read as physically weathered and situated in a plausible landscape, with natural (not studio) lighting.",
        "success_criteria": [
            "Ruins show visible weathering/erosion consistent with age",
            "Natural landscape context (sky, ground, vegetation, or terrain)",
            "Natural daylight character, not flat studio lighting",
            "No modern structures or objects in frame",
        ],
        "failure_patterns": [
            "Ruins look freshly built / unweathered",
            "No landscape context (floating in void)",
            "Lighting reads as artificial/studio",
            "Modern elements appear in the background",
        ],
        "canonical_prompt": (
            "Weathered stone ruins stand on a hillside under a wide natural sky, "
            "low warm sunlight raking across broken walls and scattered fallen "
            "blocks, dry grass around the base. Camera holds a slow static wide "
            "shot. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A static wide exterior shot of weathered ruins in a natural landscape under warm natural daylight.",
    },
    {
        "validation_id": "underground_chamber",
        "category": "underground_chamber",
        "objective": "Prove the model can render an enclosed underground space with correct claustrophobic scale and a single motivated light source.",
        "what_is_tested": "Whether the chamber reads as genuinely enclosed/underground (low ceiling, no sky, contained darkness) rather than an open exterior mislabeled as underground.",
        "success_criteria": [
            "Space reads as enclosed/underground (no sky, contained walls/ceiling)",
            "Single motivated light source (not ambient daylight flooding the space)",
            "Near-black dominant tone outside the lit area",
            "Physically plausible enclosed geometry",
        ],
        "failure_patterns": [
            "Space reads as an open exterior despite being labeled underground",
            "Fully/evenly lit rather than a single contained source",
            "Ceiling/walls are inconsistent or missing",
        ],
        "canonical_prompt": (
            "A low-ceilinged underground stone chamber, walls close on all sides, "
            "lit only by a single hanging oil lamp casting warm light a short "
            "distance before fading into near-black shadow. Camera holds a slow "
            "static shot. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A static shot of a genuinely enclosed underground chamber lit by one contained, localized light source.",
    },
    {
        "validation_id": "mechanism_detail",
        "category": "mechanism_detail",
        "objective": "Prove a close-up on a mechanical/technical artifact stays legible and does not over-generalize into a generic metal object.",
        "what_is_tested": "Whether the model renders specific, legible mechanical detail (gears, dials, fittings) rather than an abstract metallic blob, without naming any specific real artifact.",
        "success_criteria": [
            "Distinct mechanical forms are legible (gear teeth, dial edges, fittings)",
            "Material reads as aged/corroded metal, not polished or CGI-clean",
            "Camera holds steady enough for the detail to be readable",
            "No text or markings resembling a modern label/logo",
        ],
        "failure_patterns": [
            "Detail is too soft/generalized to read as mechanical",
            "Metal reads as bright, polished, or plastic-like",
            "Camera moves too fast for detail to register",
            "Modern-looking text or branding appears on the object",
        ],
        "canonical_prompt": (
            "Extreme close-up on a corroded ancient bronze mechanism, exposed "
            "gears and dials with dark green-bronze patina, catching a narrow "
            "rim of warm light along their edges. Camera holds a slow, subtle "
            "push-in. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A slow, legible close-up revealing specific corroded mechanical forms (gears, dials) under a single rim light.",
    },
    {
        "validation_id": "ancient_writing",
        "category": "ancient_writing",
        "objective": "Prove carved ancient writing/glyphs can appear as diegetic scene content without becoming a subtitle, caption, or legible modern text overlay.",
        "what_is_tested": "Whether inscriptions read as carved, physical, non-Latin/non-legible symbolic marks embedded in stone, not as a 2D text overlay.",
        "success_criteria": [
            "Carved marks read as physically carved into a stone surface (depth, shadow, wear)",
            "Marks are not legible modern text/alphabet — abstract/ancient-symbolic in character",
            "No flat overlay, caption, or subtitle-style rendering",
            "Lighting is consistent with the rest of the surface",
        ],
        "failure_patterns": [
            "Marks read as a flat 2D text overlay rather than carved stone",
            "Legible modern words or letters appear",
            "Marks resemble a caption/subtitle bar",
            "Inconsistent lighting between the marks and the surrounding stone",
        ],
        "canonical_prompt": (
            "Close-up on a weathered stone surface covered in carved ancient "
            "symbols and glyphs, worn smooth by age, catching low raking light "
            "that reveals their carved depth. Camera holds a slow, subtle pan "
            "across the surface. No people, no modern text, no modern objects."
        ),
        "expected_visual_behavior": "A slow pan across physically carved, worn, non-legible ancient glyphs revealed by raking light — never a flat text overlay.",
    },
    {
        "validation_id": "material_realism",
        "category": "material_realism",
        "objective": "Prove close-up material rendering (stone, metal, cloth) reads as physically tactile and realistic, not CGI-clean or plastic.",
        "what_is_tested": "Whether surface texture (roughness, cracking, weathering, grain) is visibly present and consistent under the scene's lighting.",
        "success_criteria": [
            "Visible surface texture/imperfection (cracks, grain, weathering)",
            "Material reads as physically real, not smooth/plastic/CGI-clean",
            "Texture responds believably to the light direction",
            "No unnaturally uniform or glossy surfaces",
        ],
        "failure_patterns": [
            "Surface is smooth/glossy/plastic-looking",
            "Texture looks like a flat repeating pattern",
            "Material doesn't respond to light direction believably",
            "Surface looks synthetic/CGI-rendered",
        ],
        "canonical_prompt": (
            "Extreme close-up on a weathered stone wall, deep texture, cracks, "
            "and mineral grain visible, warm raking light emphasizing every "
            "surface imperfection. Camera holds a slow, subtle drift across the "
            "surface. No people, no text, no modern objects."
        ),
        "expected_visual_behavior": "A slow close-up drift across a stone surface where texture, cracking, and grain are clearly readable and light-responsive.",
    },
    {
        "validation_id": "transition_consistency",
        "category": "transition_consistency",
        "objective": "Prove a clip can begin and end on a clean, holdable beat suitable for cutting to/from an adjacent scene, per Video DNA's static-cut/slow-push transition rule.",
        "what_is_tested": "Whether the clip settles into a stable, non-mid-motion final frame (or begins from one) rather than ending abruptly mid-movement.",
        "success_criteria": [
            "Clip opens or closes on a brief static hold (not mid-motion)",
            "Camera motion, if any, resolves to a stop before the clip ends",
            "Final frame is compositionally clean and stable",
            "No abrupt cut-off mid-pan/mid-push",
        ],
        "failure_patterns": [
            "Clip ends mid-movement with no settling beat",
            "Final frame is compositionally chaotic or unbalanced",
            "Camera appears to be accelerating at the cut point",
        ],
        "canonical_prompt": (
            "The camera slowly approaches a carved stone threshold and comes to "
            "a gentle, complete stop directly before it, holding the final frame "
            "steady as warm light spills through from beyond. No people, no "
            "text, no modern objects."
        ),
        "expected_visual_behavior": "A slow approach that fully resolves to a stable, held final frame at a natural cut point — a threshold.",
    },
]

_SCENES_BY_ID = {s["validation_id"]: s for s in _SCENES}


def get_all_scenes() -> list:
    """Return all validation scenes, in canonical order."""
    return list(_SCENES)


def get_scene(validation_id: str) -> dict:
    """Return one validation scene by validation_id. Raises KeyError if unknown."""
    return _SCENES_BY_ID[validation_id]


def get_categories() -> list:
    """Return the list of distinct categories covered by the suite."""
    return [s["category"] for s in _SCENES]
