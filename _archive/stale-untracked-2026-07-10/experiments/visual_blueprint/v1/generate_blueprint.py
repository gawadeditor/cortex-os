"""
Blueprint Engine v1 — generate_blueprint.py
Reads a scene from visual_package and produces a structured cinematic blueprint JSON.

Usage:
    python generate_blueprint.py <scene_id> [--output path]

Guardrails:
    - No pipeline import
    - No provider calls
    - Does not modify visual_package
"""

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
VISUAL_PACKAGE = (
    REPO_ROOT / ".hypogeum-assets" / "episodes" / "ep_000" / "visual_package_ep_000.json"
)
BLUEPRINT_VERSION = "1.0"


def load_scene(scene_id: str) -> dict:
    with open(VISUAL_PACKAGE) as f:
        package = json.load(f)
    for scene in package["scenes"]:
        if scene["scene_id"] == scene_id:
            return scene
    raise ValueError(f"Scene {scene_id!r} not found in visual package")


# ── Per-scene blueprint definitions ───────────────────────────────────────────
# Structured cinematic layer data per scene. Prompts are derived by compose_*.py.
# Content directive fields are locked from the visual package and task brief.

def _bp(scene_id, subject, setting, period, dur, aspect, continuity,
        premise, shot_type, cam_pos, subj_decl,
        light_src, light_dir, light_temp, light_feat, light_falloff,
        obs1, obs2, obs3, obs4,
        bg, ratio, breath,
        cam_verb, subj_behavior, motion_qual,
        seg0, seg4,
        lens, dof, grade,
        generic_c, specific_c,
        provider="muapi", fallback="muapi", tier=2):
    return {
        "meta": {
            "blueprint_id": f"bp_{scene_id}_v1",
            "blueprint_version": BLUEPRINT_VERSION,
            "scene_id": scene_id,
            "episode_id": "ep_000",
            "source_package": str(VISUAL_PACKAGE),
        },
        "content_directive": {
            "subject": subject,
            "setting": setting,
            "period": period,
            "duration_seconds": dur,
            "aspect_ratio": aspect,
            "continuity_notes": continuity,
        },
        "cinematic_layers": {
            "premise": {"statement": premise},
            "framing": {
                "shot_type": shot_type,
                "camera_position": cam_pos,
                "subject_declaration": subj_decl,
            },
            "lighting": {
                "source_type": light_src,
                "direction": light_dir,
                "color_temperature": light_temp,
                "illuminated_feature": light_feat,
                "falloff": light_falloff,
            },
            "material_detail": {
                "observation_1": obs1,
                "observation_2": obs2,
                "observation_3": obs3,
                **({"observation_4": obs4} if obs4 else {}),
            },
            "spatial_composition": {
                "background": bg,
                "subject_frame_ratio": ratio,
                "breathing_room": breath,
            },
            "motion": {
                "camera_verb": cam_verb,
                "subject_behavior": subj_behavior,
                "motion_qualifier": motion_qual,
            },
            "time_segmentation": {
                "segment_0_4s": seg0,
                "segment_4_8s": seg4,
            },
            "technical_tags": {
                "resolution": "4K",
                "framerate": "24fps",
                "aspect_ratio": aspect,
                **({"lens_type": lens} if lens else {}),
                "depth_of_field": dof,
                "color_grade": grade,
            },
            "constraints": {
                "generic": generic_c,
                "scene_specific": specific_c,
            },
        },
        "provider_hints": {
            "primary_provider": provider,
            "fallback_provider": fallback,
            "tier": tier,
        },
    }


BLUEPRINTS = {

"s01": _bp(
    scene_id="s01",
    subject="sponge diver surfacing, Antikythera coastline",
    setting="Aegean Sea surface, rocky limestone coastline",
    period="1900 CE",
    dur=8, aspect="16:9",
    continuity="First scene — establishes the discovery event; Aegean ultramarine palette carries forward",
    premise="A body breaks the surface of the Aegean for the last time before the world changes.",
    shot_type="wide_establishing",
    cam_pos="low angle at sea surface level, diver at frame-left",
    subj_decl=(
        "a sponge diver in rough water-darkened canvas gear erupts through the wave surface "
        "against the pale limestone Antikythera coastline, seawater cascading from hemp rope "
        "and saturated fabric"
    ),
    light_src="directional",
    light_dir="overhead — near-perpendicular noon",
    light_temp="bleached noon white",
    light_feat=(
        "wave crests bleaching to white foam and the diver's canvas shirt at the shoulder; "
        "hard reflections fracturing across the fractured surface"
    ),
    light_falloff=(
        "Aegean water deepens to intense ultramarine below the scatter layer; "
        "limestone cliff holds iron-oxide warmth in its deep fissures only"
    ),
    obs1=(
        "rough canvas shirt — salt-bleached along the shoulders, charcoal-heavy where seawater "
        "saturates the fabric fully; layers of salt service visible in the fibre texture"
    ),
    obs2=(
        "hemp rope at the diver's waist — still beaded with water, fibres darkened by salt "
        "immersion, individual strands visible in the noon light"
    ),
    obs3=(
        "pale limestone cliff face at frame-right — iron-oxide streaks running vertically, "
        "dried sea-salt crust above the waterline, shadow gathering only in deep fissures"
    ),
    obs4=(
        "Aegean water surface — intense ultramarine in the deeper scatter layer, wave crests "
        "bleached to near-white foam at the diver's emergence point"
    ),
    bg=(
        "Aegean fills three-quarters of the frame in intense ultramarine; "
        "limestone cliff anchors the right edge; open horizon at upper-left"
    ),
    ratio="diver occupies upper-left quarter of frame at emergence",
    breath="open horizon at upper-left, sea extending right",
    cam_verb="slow_pan",
    subj_behavior="diver surfaces and bobs at frame-left as the pan carries gaze right",
    motion_qual=(
        "pan carries gaze across the turbulent wake and out toward the open sea and horizon"
    ),
    seg0=(
        "diver's torso clears the water surface, canvas gear shedding heavy sheets of seawater, "
        "wave disturbance radiating outward from the point of emergence"
    ),
    seg4=(
        "the disturbed water surface settles into open ultramarine, the diver receding to "
        "frame-left as the wake calms and the Aegean horizon opens fully at the right"
    ),
    lens=None,
    dof="wide — surface to horizon",
    grade="bleached noon white against ultramarine blue",
    generic_c=["without modern watercraft", "without contemporary diving equipment"],
    specific_c=["no wetsuit", "no modern coastline infrastructure", "no synthetic rope"],
    provider="muapi", fallback="muapi", tier=1,
),

"s02": _bp(
    scene_id="s02",
    subject="corroded bronze gear fragments on examination table",
    setting="Athens museum examination table, 1902",
    period="1902 CE Athens",
    dur=8, aspect="16:9",
    continuity="Establishes the mechanism patina palette — green-brown marine, copper-orange at edges; match in all subsequent fragment scenes",
    premise="The moment a two-thousand-year-old machine introduces itself.",
    shot_type="extreme_close_up",
    cam_pos="locked at bench surface level, fragments filling lower two-thirds of frame",
    subj_decl=(
        "corroded bronze gear fragments arranged on a grey-beige marble examination table "
        "in 1902 Athens, each piece carrying dense green-brown marine patina with "
        "copper-orange exposed at sheared edges where ancient alloy fractured clean"
    ),
    light_src="directional",
    light_dir="upper left",
    light_temp="warm amber",
    light_feat=(
        "corroded tooth faces and fragment surfaces at a low oblique angle, "
        "casting micro-shadow into every pit and fissure"
    ),
    light_falloff=(
        "surrounding table surface recedes into warm shadow beyond the lamp radius; "
        "no secondary fill — hard warmth to dark"
    ),
    obs1=(
        "dense green-brown marine patina layered concentrically across each fragment face — "
        "heavy oxidation built from decades of seawater immersion, deepest in the protected valleys"
    ),
    obs2=(
        "copper-orange exposed at sheared edges where ancient alloy fractured clean — "
        "the internal metal visible where the patina crust has not yet reformed"
    ),
    obs3=(
        "fine calcite crust deposits catching lamp warmth as pale cream against the dark bronze field — "
        "secondary mineralisation from seawater carbonate"
    ),
    obs4=(
        "grey-beige marble table surface stained with iron bleed-out where fragments rested "
        "during recovery — rust-ochre halos around each fragment's resting position"
    ),
    bg=(
        "stone table surface in warm shadow beyond the lamp radius; "
        "no background beyond the immediate bench context"
    ),
    ratio="fragments occupy centre-to-left of frame across the lower half",
    breath="upper right corner falls to warm table shadow",
    cam_verb="static",
    subj_behavior="motionless",
    motion_qual=(
        "the eye traces from fragment to fragment; the lamp does not shift; "
        "only the depth of shadow changes as attention moves"
    ),
    seg0=(
        "the central fragment fills the lower half of frame as lamp light resolves gear tooth "
        "surface texture, patina detail sharpening from the brightest lit face inward to shadow"
    ),
    seg4=(
        "light stabilises at full amber warmth, shadow edges of each tooth deepening "
        "as the eye adjusts to the micro-scale precision"
    ),
    lens="macro",
    dof="shallow — fragment surface to stone table",
    grade="warm amber against cool grey marble",
    generic_c=["without modern display labelling", "without institutional context"],
    specific_c=["no fluorescent fill", "no clinical lighting", "no modern storage containers"],
    provider="muapi", fallback="muapi", tier=1,
),

"s03": _bp(
    scene_id="s03",
    subject="ancient bronze interlocking gear teeth",
    setting="dark neutral background",
    period="1st century BCE — reconstructed",
    dur=8, aspect="16:9",
    continuity="Match gear patina from s02 — green-brown valleys, copper-orange contact points",
    premise="Precision this exact should not exist.",
    shot_type="macro",
    cam_pos="at tooth-apex level, traversing the mesh line from left to right",
    subj_decl=(
        "ancient bronze interlocking gear teeth meshing in absolute contact, each tooth face "
        "carrying green-brown marine patina with bright copper-orange at the apex "
        "of the engagement point where metal meets metal"
    ),
    light_src="raking_side",
    light_dir="left",
    light_temp="warm amber",
    light_feat=(
        "the leading edge of each tooth burning hard along its face; "
        "the contact point between the two meshing gears holding a micro-highlight "
        "where patina has been worn away by mechanical pressure over centuries"
    ),
    light_falloff=(
        "deep shadow into the valley between each tooth pair; "
        "no ambient fill — pure black at the base of each valley"
    ),
    obs1=(
        "deep green oxidation in the protected valleys — marine patina at its densest "
        "where mechanical pressure and light have never reached"
    ),
    obs2=(
        "layered brown-to-orange at the tooth mid-face where patina transitions "
        "from full depth to thinning oxidation crust"
    ),
    obs3=(
        "raw copper colour at the tooth-tip contact points — the engagement surface "
        "worn clean by two thousand years of mechanical contact"
    ),
    obs4=None,
    bg="pure unlit black — no texture, no gradient, no depth reference",
    ratio="gear teeth occupy the central horizontal band of the frame",
    breath="pure black void above and below the tooth register",
    cam_verb="slow_pan",
    subj_behavior="gear teeth fixed — only the pan moves the frame across the mesh line",
    motion_qual="pan follows the contact arc of the interlocked teeth from left to right",
    seg0=(
        "the leftmost visible teeth resolve from left-edge darkness, meshing detail emerging "
        "from the engagement point inward, micro-highlight at contact burning amber"
    ),
    seg4=(
        "the rightmost teeth enter the frame as the leftmost teeth recede to void, "
        "the full mesh contact arc resolved in amber raking light"
    ),
    lens="macro",
    dof="razor-thin — tooth apex to valley",
    grade="amber highlight against pure black void",
    generic_c=["without modern machining", "without contemporary materials"],
    specific_c=["no background fill", "no lens flare", "no scale reference"],
    provider="muapi", fallback="muapi", tier=2,
),

"s04": _bp(
    scene_id="s04",
    subject="lone craftsman at workbench",
    setting="ancient Greek workshop, Hellenistic period",
    period="3rd century BCE",
    dur=8, aspect="16:9",
    continuity="Establishes the workshop lamp palette — terracotta oil lamp, amber fire; match in all workshop scenes",
    premise="A mind working alone at the edge of what the ancient world believed possible.",
    shot_type="medium",
    cam_pos="at bench height, several metres back, craftsman centred",
    subj_decl=(
        "the silhouette of an ancient Greek craftsman bent over a grey limestone workbench "
        "in a Hellenistic workshop, his outline sharp against the warm amber pool "
        "of a terracotta oil lamp burning at the bench edge"
    ),
    light_src="oil_lamp",
    light_dir="bench centre — radiating outward",
    light_temp="warm amber",
    light_feat=(
        "bronze tools and the work surface in a tight amber cone; "
        "the craftsman's near shoulder catching warmth while the far side recedes to silhouette"
    ),
    light_falloff=(
        "workshop columns and ceiling recede to deep shadow beyond one metre; "
        "rapid falloff in all directions — pure darkness at the room's perimeter"
    ),
    obs1=(
        "fired terracotta oil lamp — single-wick form, warm amber flame the scene's "
        "exclusive illumination in a radius of roughly one metre"
    ),
    obs2=(
        "grey limestone workbench surface — bronze filings catching the lamp warmth "
        "as scattered pale-gold points against the stone"
    ),
    obs3=(
        "linen craftsman's robe — light fabric holding lamp warmth at the near shoulder, "
        "receding to silhouette on the far side away from the flame"
    ),
    obs4=None,
    bg=(
        "workshop darkness fills the perimeter and ceiling — columns recede to deep shadow "
        "beyond the lamp cone; the bench and figure are the sole lit elements"
    ),
    ratio="craftsman and bench occupy the central third of the frame",
    breath="workshop darkness expands at all frame edges beyond the lamp cone",
    cam_verb="static",
    subj_behavior="craftsman's hands move over the device; the figure does not look up",
    motion_qual="lamp flame holds steady; micro-glints from bronze filings catch light at the bench surface",
    seg0=(
        "craftsman's hands move over the device, lamp flame holds steady, "
        "micro-glints from bronze filings catching light at the bench surface"
    ),
    seg4=(
        "figure holds position, head angled toward the work, "
        "workshop darkness deepening behind as the eye adjusts to the lamp cone"
    ),
    lens=None,
    dof="medium — figure to bench surface",
    grade="warm amber fire against deep workshop shadow",
    generic_c=["without modern tools", "without electric lighting"],
    specific_c=["no background detail beyond lamp radius", "no direct face visibility", "no synthetic workshop materials"],
    provider="muapi", fallback="muapi", tier=2,
),

"s05": _bp(
    scene_id="s05",
    subject="Valerios Stais examining corroded bronze lump",
    setting="Athens museum storeroom, 1902",
    period="1902 CE",
    dur=8, aspect="16:9",
    continuity="First of two Stais recognition scenes (s05, s28) — establish the gaslit storeroom palette here",
    premise="The moment of discovery that the academic world will not believe.",
    shot_type="medium",
    cam_pos="at storeroom aisle level, archaeologist at frame centre",
    subj_decl=(
        "a Greek archaeologist in a heavy dark wool suit leaning toward a corroded bronze mass "
        "on a wooden shelf in an Athens museum storeroom, 1902, his face angled "
        "into dim amber gaslight falling from a wall bracket above and to the left"
    ),
    light_src="gaslight",
    light_dir="upper left — wall bracket",
    light_temp="warm amber (dim)",
    light_feat=(
        "the wool suit surface at the lapels and collar, and the bronze fragment's "
        "green-brown encrusted surface as warm ochre overlay"
    ),
    light_falloff=(
        "storeroom shelves behind carry artefacts in shadow — the background falls "
        "to near-black beyond the gaslight radius; no secondary fill"
    ),
    obs1=(
        "heavy dark wool suit — woven close, the lapels and collar holding the warmest "
        "lamp reflection; the fabric darkening from amber to near-black away from the light source"
    ),
    obs2=(
        "corroded bronze mass — green-brown outer crust of marine encrustation "
        "with an ochre overlay where gaslight warms the surface"
    ),
    obs3=(
        "wooden museum storeroom shelving — dark timber with age, other artefacts in shadow: "
        "terracotta fragments, wrapped bundles, wooden crates forming an unlit depth"
    ),
    obs4=None,
    bg=(
        "storeroom shelves form a dense but unlit background behind and around the archaeologist; "
        "the bronze fragment is the brightest object in the scene"
    ),
    ratio="archaeologist occupies two-thirds of frame width, centred",
    breath="upper right and lower left fall to storeroom shadow",
    cam_verb="static",
    subj_behavior="the lean toward the fragment is held — no movement except a subtle breath",
    motion_qual="attention tightening on the object; the lean deepens fractionally in the second half",
    seg0=(
        "figure's posture resolves from the gaslit scene, bronze fragment's encrusted surface "
        "catching amber wash, storeroom background establishing as deep shadow depth"
    ),
    seg4=(
        "the archaeologist's attention tightens — the lean deepens fractionally, "
        "the bronze fragment remaining the light's focal point, storeroom still around him"
    ),
    lens=None,
    dof="figure sharp — shelf background progressively soft",
    grade="warm amber gaslight against cool dark storeroom",
    generic_c=["without modern fluorescent lighting", "without institutional display framing"],
    specific_c=["no electric light sources", "no contemporary clothing", "no anachronistic storeroom objects"],
    provider="muapi", fallback="muapi", tier=2,
),

"s06": _bp(
    scene_id="s06",
    subject="European scholars dismissing document",
    setting="formal European academic hall, 1902",
    period="1902 CE",
    dur=8, aspect="16:9",
    continuity="Cool daylight palette — contrasts with the warm gaslight of s05; no warm tones in this scene",
    premise="A room full of certain men confronting evidence they have decided not to understand.",
    shot_type="wide",
    cam_pos="elevated at far end of hall, looking toward the table",
    subj_decl=(
        "five European scholars in formal 1902 academic dress — dark frock coats, high collars, "
        "watch chains — arranged around a rectangular dark-polished-wood table in a formal academic hall, "
        "one figure pushing a cream document toward the table centre while others gesture dismissal"
    ),
    light_src="diffuse",
    light_dir="left — tall windows",
    light_temp="cool grey-white",
    light_feat=(
        "the table surface and scholar figures in flat even illumination; "
        "near-shadowless in the diffuse quality — no warmth anywhere"
    ),
    light_falloff=(
        "dark wood panelling and high plaster ceiling recede to cool shadow at the room's far end; "
        "no warm fill from any source"
    ),
    obs1=(
        "dark Victorian wool frock coats and high collars — heavy fabric, "
        "watch chains at the waistcoats catching cool daylight as pale metal points"
    ),
    obs2=(
        "dark polished wood table surface — the cream document resting on it "
        "as a pale contrast against the dark field"
    ),
    obs3=(
        "dark wood panelling and high plaster ceiling — the architectural detail "
        "receding to cool shadow at the room's far end"
    ),
    obs4=None,
    bg=(
        "the rectangular table occupies the frame centre; tall windows at frame-left "
        "are the only light source; the hall's far end recedes to cool shadow"
    ),
    ratio="table and five figures fill the frame centre in a wide composition",
    breath="hall extends to the right and behind at the far wall",
    cam_verb="static",
    subj_behavior="scholars hold dismissive postures; one figure begins to rise at the near end",
    motion_qual="one figure at the near end rises fractionally — a posture of departure",
    seg0=(
        "full table composition holds, central document visible as the focal object, "
        "scholars' postures of dismissal legible in their arrangement"
    ),
    seg4=(
        "one figure at the near end rises fractionally, posture of departure; "
        "the document remains at the table centre, unresolved"
    ),
    lens=None,
    dof="wide — table to hall background",
    grade="cool grey-white diffuse daylight, dark polished wood",
    generic_c=["without modern furniture", "without contemporary academic context"],
    specific_c=["no warm ambient fill", "no artificial lighting", "no post-Victorian architectural detail"],
    provider="muapi", fallback="muapi", tier=2,
),

"s07": _bp(
    scene_id="s07",
    subject="ancient Greek mathematician with papyrus scrolls",
    setting="Hellenistic library interior",
    period="3rd century BCE",
    dur=8, aspect="16:9",
    continuity="Establishes marble column palette — pale Pentelic white with grey veining; match in s11",
    premise="A man who understood the sky as a machine, and built the proof.",
    shot_type="medium",
    cam_pos="at library floor level several metres back, scholar at composition centre",
    subj_decl=(
        "an ancient Greek scholar in a white wool chiton standing at a dark-stained cedarwood "
        "reading stand in a Hellenistic library interior, a papyrus scroll partially unrolled "
        "before him, his posture erect with the authority of a man who has worked through the problem"
    ),
    light_src="diffuse",
    light_dir="from column openings at far end",
    light_temp="warm amber",
    light_feat=(
        "white chiton at the shoulder and chest in broad pale shafts; "
        "papyrus scroll surface catching column-diffused daylight as warm cream"
    ),
    light_falloff=(
        "lower half of the chiton falls to cooler shadow; "
        "library shelves at the perimeter hold warm dimness; no hard falloff"
    ),
    obs1=(
        "white wool chiton — natural fibre with visible texture, holding warm amber daylight "
        "at the near shoulder and chest, cooling toward shadow at the lower half"
    ),
    obs2=(
        "pale Pentelic marble columns — faint grey veining, surfaces worn smooth from centuries "
        "of use, holding daylight warmly on their facing surfaces"
    ),
    obs3=(
        "dark-stained cedarwood reading stand — iron fittings, the stand's upper face "
        "catching column-diffused daylight as warm ochre"
    ),
    obs4=(
        "ochre-cream papyrus scroll — dark ink geometric diagrams visible in the lit zone: "
        "circles, angles, numerical notation in careful Greek script"
    ),
    bg=(
        "marble columns at the far end frame the light source; "
        "library shelves in warm shadow at the perimeter; the reading stand at composition centre"
    ),
    ratio="scholar and reading stand occupy the central third of frame",
    breath="marble columns frame left and right; library depth extends behind",
    cam_verb="static",
    subj_behavior="scholar faces three-quarters away from frame; hand moves to the scroll in the second half",
    motion_qual="scholar's hand moves to a point on the scroll diagram in the second half",
    seg0=(
        "scholar, stand, scroll, and column-light establish as a unit; "
        "daylight shaft holds warm amber consistency"
    ),
    seg4=(
        "scholar's hand moves to a point on the scroll diagram, "
        "papyrus surface catching the diffuse light as warm cream"
    ),
    lens=None,
    dof="medium — scholar to column background",
    grade="warm amber daylight against cool marble shadow",
    generic_c=["without modern materials", "without contemporary academic context"],
    specific_c=["no artificial light sources", "no direct-to-camera gaze", "no non-Hellenistic architectural detail"],
    provider="muapi", fallback="muapi", tier=2,
),

"s08": _bp(
    scene_id="s08",
    subject="mechanism fragments on dark surface, question implied",
    setting="dark museum surface, overhead view",
    period="contemporary",
    dur=8, aspect="16:9",
    continuity="Match fragment patina from s02 — green-brown marine, copper-orange cross-sections",
    premise="Objects that have survived two thousand years to ask a single question.",
    shot_type="overhead",
    cam_pos="directly above at high angle, fragments filling frame centre",
    subj_decl=(
        "corroded bronze mechanism fragments arranged on a matte black surface, "
        "shot from directly above, the composition implying a radial arrangement "
        "that suggests interrogation without literalism"
    ),
    light_src="spotlight_overhead",
    light_dir="directly above",
    light_temp="warm amber",
    light_feat=(
        "fragments at the spotlight cone's centre, illuminating the patina surface "
        "and casting hard-edged shadows inward toward the composition's centre"
    ),
    light_falloff=(
        "sharp falloff at the cone's edge — surrounding field drops to pure black; "
        "matte black surface absorbs all scattered light"
    ),
    obs1=(
        "dense green-brown marine patina across each fragment face — "
        "oxidation at its heaviest where seawater maintained contact longest"
    ),
    obs2=(
        "pale copper-orange cross-sections at the fragment edges — "
        "where ancient alloy fractured, the internal composition exposed as bright contrast"
    ),
    obs3=(
        "fine calcite deposits as pale cream crusts in the protected internal angles "
        "of each fragment — secondary carbonate mineralisation"
    ),
    obs4=(
        "matte black surface beneath the fragments — no reflection, no sheen, "
        "absorbing all light that escapes the bronze surfaces"
    ),
    bg=(
        "pure unlit black beyond the tight spotlight cone; "
        "no background detail, no surface texture visible beyond the fragments"
    ),
    ratio="fragments occupy the spotlight cone at the frame centre",
    breath="pure black expands at all frame edges beyond the cone",
    cam_verb="static",
    subj_behavior="motionless",
    motion_qual=(
        "the only change is the spotlight's centre burning brightest at the most-patinated "
        "fragment face, surrounding darkness deepening as the eye adjusts"
    ),
    seg0=(
        "overhead spotlight establishes — fragments emerge from surrounding darkness "
        "as the cone of light resolves patina textures, shadow lines hardening"
    ),
    seg4=(
        "composition holds, spotlight centre burning brightest at the most-patinated "
        "fragment face, surrounding darkness deepening"
    ),
    lens="overhead",
    dof="shallow — fragment surfaces across the overhead plane",
    grade="amber spotlight centre against pure black",
    generic_c=["without modern display labelling", "without institutional context"],
    specific_c=["no side fill", "no reflected light from surrounding surfaces", "no background texture"],
    provider="muapi", fallback="muapi", tier=2,
),

"s09": _bp(
    scene_id="s09",
    subject="Aegean coastline, open horizon",
    setting="Greek island cliff edge, timeless Mediterranean",
    period="timeless Mediterranean",
    dur=8, aspect="16:9",
    continuity="Golden hour palette — amber-copper sea and limestone; echoes s31 closing shot",
    premise="The sea that kept the secret for two thousand years.",
    shot_type="wide",
    cam_pos="low angle at cliff edge level, cliff running lower-left to upper-right",
    subj_decl=(
        "a Greek island cliff edge of pale sun-bleached limestone running from the lower-left "
        "frame corner toward the horizon at upper-right, the Aegean Sea spreading outward "
        "to the full horizon in intense cerulean and copper-gold"
    ),
    light_src="golden_hour",
    light_dir="right — very low angle",
    light_temp="warm copper-amber",
    light_feat=(
        "cliff face limestone in raking detail — every fissure and salt-crust ridge "
        "drawn into high relief; sea surface fracturing the same warm light "
        "into a field of scattered amber points"
    ),
    light_falloff=(
        "sea deepens from copper-gold at the horizon to cerulean in the shadow areas; "
        "cliff base at the wave-cut zone holds cool shadow"
    ),
    obs1=(
        "pale sun-bleached limestone — fractured and layered, salt-crusted at the wave-cut base, "
        "amber-warm at the cliff's upper face in the golden-hour raking light"
    ),
    obs2=(
        "dry Mediterranean scrub clinging to the cliff face — grey-green sparse vegetation, "
        "gilded at the leaf tips by the near-horizontal golden light"
    ),
    obs3=(
        "Aegean water surface — deep cerulean blue in the middle distance, "
        "transitioning to warm copper-gold at the horizon line where the low sun strikes"
    ),
    obs4=None,
    bg=(
        "Aegean Sea fills the right two-thirds of frame and the full background; "
        "open horizon runs across the upper frame"
    ),
    ratio="cliff edge occupies the lower-left diagonal; sea fills the rest",
    breath="open horizon at upper-right; sea expands in the pan",
    cam_verb="slow_pan",
    subj_behavior="cliff and sea are static; the pan reveals the full horizon",
    motion_qual="pan carries gaze from cliff face detail toward the open sea and full horizon beyond",
    seg0=(
        "the cliff face resolves in close detail, amber side light drawing limestone texture "
        "into sharp relief, the Aegean visible behind establishing depth"
    ),
    seg4=(
        "the cliff falls to frame-left as the full copper-gold sea surface expands across "
        "the frame, the Aegean horizon completing at the right edge"
    ),
    lens=None,
    dof="wide — cliff face to horizon",
    grade="amber-copper golden hour against cerulean-deep blue sea",
    generic_c=["without modern coastal development", "without contemporary signage"],
    specific_c=["no artificial light sources", "no human figures", "no non-Mediterranean vegetation"],
    provider="muapi", fallback="muapi", tier=2,
),

"s10": _bp(
    scene_id="s10",
    subject="Roman lawyer reading papyrus scroll",
    setting="Roman villa atrium",
    period="1st century BCE Rome",
    dur=8, aspect="16:9",
    continuity="Establishes Roman villa palette — warm atrium daylight, white wool toga; match in s11, s12",
    premise="A man of the Roman world reading a text that proved something they had already seen.",
    shot_type="medium",
    cam_pos="at atrium floor level, figure at the impluvium centre",
    subj_decl=(
        "a Roman man in a heavy white wool toga with a narrow purple stripe "
        "reading from an ochre-cream papyrus scroll partially unrolled in his hands, "
        "standing in a marble-floored villa atrium"
    ),
    light_src="diffuse",
    light_dir="overhead — impluvium opening",
    light_temp="warm amber (atrium daylight)",
    light_feat=(
        "white toga surface and papyrus scroll face in a broad warm overhead column; "
        "thin column shadows at regular intervals across the pale marble floor"
    ),
    light_falloff=(
        "atrium columns at the perimeter hold warm dimness; "
        "no artificial secondary fill; the impluvium column is the sole light source"
    ),
    obs1=(
        "heavy wool toga — white with natural fibre variation visible in the draped folds, "
        "the purple stripe catching overhead warmth as deep violet"
    ),
    obs2=(
        "ochre-cream papyrus scroll — slightly curling at the rolled edges, "
        "dense ink text in careful Roman script visible in the lit zone"
    ),
    obs3=(
        "polished marble atrium floor — pale cream with grey veining, "
        "thin column shadows falling at regular intervals"
    ),
    obs4=None,
    bg=(
        "marble columns surround the atrium at regular intervals; "
        "impluvium opening provides the single overhead light column; "
        "colonnaded walkway recedes to warm shadow"
    ),
    ratio="figure occupies centre-left of frame",
    breath="colonnade extends right; impluvium light opens overhead",
    cam_verb="static",
    subj_behavior="figure faces slightly away from direct frame; hands shift scroll in second half",
    motion_qual="figure's hands shift the scroll to reveal more text",
    seg0=(
        "figure, scroll, and atrium daylight establish; "
        "impluvium light column holds consistent warmth across the toga surface"
    ),
    seg4=(
        "figure's hands shift the scroll to reveal more text, "
        "papyrus surface catching the warm overhead light as warm cream"
    ),
    lens=None,
    dof="medium — figure to atrium columns",
    grade="warm atrium daylight, white wool and ochre-cream tones",
    generic_c=["without modern materials", "without direct-to-camera gaze"],
    specific_c=["no contemporary objects", "no artificial fill", "no non-Roman architectural context"],
    provider="muapi", fallback="muapi", tier=2,
),

"s11": _bp(
    scene_id="s11",
    subject="Cicero examining small bronze spherical instrument",
    setting="Roman villa, marble colonnaded portico",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match Roman villa palette from s10; match instrument patina from s02",
    premise="A Roman senator holding in his hands something that should not yet exist.",
    shot_type="medium_close_up",
    cam_pos="at portico column level, senator at frame centre, instrument at focal point",
    subj_decl=(
        "a Roman senator in a heavy white wool toga with a wide purple stripe "
        "examining a small bronze spherical instrument held in both open palms, "
        "in the colonnaded portico of a marble villa"
    ),
    light_src="diffuse",
    light_dir="overhead — open sky above portico",
    light_temp="warm amber (atrium daylight)",
    light_feat=(
        "white toga's upper folds and the bronze instrument's curved face; "
        "the sphere's surface showing green-brown patina with worn copper-orange highlights "
        "at the raised features"
    ),
    light_falloff=(
        "marble columns at the portico edge hold daylight warmly on facing surfaces, "
        "shadows deepening on their far sides; no artificial secondary fill"
    ),
    obs1=(
        "heavy wool toga with wide purple stripe — the purple catching warm overhead light "
        "as deep violet, the white wool draped in heavy folds"
    ),
    obs2=(
        "small bronze spherical instrument — green-brown patina on the curved face "
        "with worn copper-orange highlights at the raised features"
    ),
    obs3=(
        "pale cream-white marble columns — worn at the base where sandal traffic "
        "has smoothed the stone, column shafts holding daylight warmly on their facing surfaces"
    ),
    obs4=None,
    bg=(
        "marble columns at the portico edge frame left and right; "
        "open sky above provides the light source; "
        "the senator is at composition centre, the instrument the focal point"
    ),
    ratio="senator and instrument occupy the central half of frame",
    breath="columns frame both sides; open sky above",
    cam_verb="static",
    subj_behavior="senator looks down at the instrument; the instrument rests in open palms, not gripped",
    motion_qual="the senator's gaze remains on the instrument throughout; bronze surface catching warm daylight at highest-relief features",
    seg0=(
        "figure, instrument, and column-framed daylight establish; "
        "bronze sphere's surface detail resolves in warm light, patina texture sharpening"
    ),
    seg4=(
        "senator's gaze remains on the instrument, bronze surface catching warm daylight "
        "at the highest-relief features, surrounding marble holding pale warmth"
    ),
    lens=None,
    dof="shallow-medium — instrument surface to senator's hands",
    grade="warm amber against marble cream-white",
    generic_c=["without modern objects", "without contemporary clothing"],
    specific_c=["no electrical light sources", "no modern backgrounds", "no non-Roman objects"],
    provider="muapi", fallback="muapi", tier=2,
),

"s12": _bp(
    scene_id="s12",
    subject="two bronze astronomical instruments on Roman shelf",
    setting="Roman villa display shelf",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match instrument patina from s11; match Roman interior palette from s10",
    premise="Two of them. Side by side. The shock of the second makes the first extraordinary.",
    shot_type="wide_medium",
    cam_pos="at shelf height, instruments centred and balanced in frame",
    subj_decl=(
        "two small bronze geared astronomical instruments resting on a Roman polished marble shelf, "
        "each approximately the size of a large fist, their patinated surfaces showing "
        "identical conditions of weathering"
    ),
    light_src="raking_side",
    light_dir="left",
    light_temp="warm amber",
    light_feat=(
        "both instruments illuminated in identical warm tones, "
        "their twin shadows falling parallel to the right — "
        "confirming the impossible fact of duplication"
    ),
    light_falloff=(
        "fresco wall behind holds warm ochre-red; "
        "no hard falloff — the side light illuminates both instruments and the shelf evenly"
    ),
    obs1=(
        "both instruments carry identical green-brown marine patina — "
        "even oxidation across the two objects suggesting they are contemporaneous"
    ),
    obs2=(
        "polished marble shelf with fine grey veining — "
        "the shelf surface holding the raking amber warmly, twin instrument shadows falling parallel"
    ),
    obs3=(
        "Roman plaster wall behind — warm ochre-red fresco, "
        "the wall surface providing a warm ground for both instruments"
    ),
    obs4=None,
    bg=(
        "warm Roman plaster frescoed in dark red and ochre fills the background; "
        "the shelf is the only horizontal surface in frame"
    ),
    ratio="two instruments sit two hands-width apart, neither dominant — balanced composition",
    breath="fresco wall extends at both sides equally",
    cam_verb="static",
    subj_behavior="motionless — the fact of two instruments is the entire content",
    motion_qual="composition holds without change — the two instruments at identical angles and patination",
    seg0=(
        "both instruments establish in the frame simultaneously, side light resolving their "
        "surface patina in matching warmth, twin shadows at identical angles"
    ),
    seg4=(
        "the composition holds without change — the fact of two instruments together "
        "is the entire content of the scene"
    ),
    lens=None,
    dof="medium — instruments to fresco wall",
    grade="amber side light against marble white and fresco ochre-red",
    generic_c=["without modern display labels", "without institutional context"],
    specific_c=["no asymmetric framing", "no focus differential between the two objects", "no modern shelf materials"],
    provider="muapi", fallback="muapi", tier=1,
),

"s13": _bp(
    scene_id="s13",
    subject="ancient road forking in two directions",
    setting="Hellenistic Mediterranean road, open landscape",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="No specific object continuity — Mediterranean landscape palette: pale limestone, grey-green scrub",
    premise="The same question asked twice, and answered differently.",
    shot_type="wide",
    cam_pos="at road surface level slightly elevated, fork at frame centre",
    subj_decl=(
        "an ancient grey limestone road forking into two diverging paths across a Mediterranean "
        "landscape, the paving stones worn smooth by centuries of foot and wheel traffic, "
        "joints filled with pale dried soil"
    ),
    light_src="directional",
    light_dir="overhead — near-vertical noon",
    light_temp="bleached noon white",
    light_feat=(
        "pressing shadows directly beneath every stone and scrub plant; "
        "the landscape bleaching toward white at the sky horizon"
    ),
    light_falloff=(
        "dusty grey-green Mediterranean scrub holds minimum shadow in the vertical noon light; "
        "no soft-light zones — harsh flat illumination"
    ),
    obs1=(
        "local grey limestone paving — worn smooth by centuries of foot and wheel traffic, "
        "joints filled with pale dried soil, faint wheel rut along the left branch's edge"
    ),
    obs2=(
        "dry Mediterranean scrub bordering both paths — dusty grey-green low vegetation, "
        "stem shadows minimal in the vertical noon light"
    ),
    obs3=(
        "pale bleached sky horizon — the landscape bleaching toward white at its upper edge "
        "under the noon intensity; no clouds"
    ),
    obs4=None,
    bg=(
        "both paths narrow toward their respective horizons; "
        "no figures, no structures beyond the road itself; open sky above"
    ),
    ratio="road fork occupies the lower two-thirds of frame",
    breath="both path horizons extend at upper-left and upper-right",
    cam_verb="static",
    subj_behavior="empty road at midday — nothing moves",
    motion_qual="the silence of an empty road at midday — the scene holds without movement or change",
    seg0=(
        "road fork establishes — both branches visible from the splitting point forward, "
        "noon light pressing the stone surface flat and pale, scrub shadow minimal"
    ),
    seg4=(
        "scene holds, the two paths receding in parallel toward separate horizons, "
        "the silence of an empty road at midday"
    ),
    lens=None,
    dof="wide — near stone to far horizon",
    grade="bleached noon white, grey-green Mediterranean scrub, pale sky",
    generic_c=["without human figures", "without modern road markings"],
    specific_c=["no modern vehicles or signage", "no non-Mediterranean vegetation", "no contemporary road infrastructure"],
    provider="muapi", fallback="muapi", tier=2,
),

"s14": _bp(
    scene_id="s14",
    subject="mechanism gear fragment with calibration marks",
    setting="forensic examination surface, contemporary",
    period="contemporary analysis",
    dur=8, aspect="16:9",
    continuity="Match fragment patina from s02 — but lit with clinical white for forensic contrast",
    premise="Evidence submitted to a court that is still deliberating.",
    shot_type="extreme_close_up",
    cam_pos="directly above at macro level, fragment face turned to show calibration marks",
    subj_decl=(
        "a corroded bronze gear fragment filling the frame, its face turned to show "
        "the calibration marks and tooth spacing that define its impossibility"
    ),
    light_src="spotlight_overhead",
    light_dir="directly above",
    light_temp="clinical white",
    light_feat=(
        "full forensic legibility across the fragment surface — every graduation mark, "
        "every tooth edge, every pit in the patina rendered in flat accurate detail"
    ),
    light_falloff=(
        "even shadowless coverage eliminating raking drama; "
        "clinical white reveals the full oxidation stratigraphy without atmospheric distortion"
    ),
    obs1=(
        "deep green marine patina in the protected valleys — "
        "oxidation at its deepest in the shadow zones between calibration graduations"
    ),
    obs2=(
        "mid-brown at intermediate faces where slight surface abrasion has thinned the patina layer"
    ),
    obs3=(
        "fine graduation lines scored into the metal — depth and regularity visible "
        "as darker grooves against the patina field"
    ),
    obs4=(
        "pale copper cross-section at the fracture edge — "
        "internal alloy composition exposed where the fragment broke from the mechanism"
    ),
    bg=(
        "the fragment fills the frame face-on; "
        "clinical white light covers the surface evenly without shadow casting; "
        "dark background at the extreme corners only"
    ),
    ratio="fragment fills the frame",
    breath="dark corners at the extreme frame edges",
    cam_verb="static",
    subj_behavior="motionless",
    motion_qual="graduations visible in flat white illumination as precise as the instrument that made them",
    seg0=(
        "clinical white light establishes full forensic legibility across the fragment surface, "
        "calibration marks resolving from the central graduation field outward to the tooth edges"
    ),
    seg4=(
        "the light holds, the fragment completely still, graduations visible "
        "in flat white illumination as precise as the instrument that made them"
    ),
    lens="macro",
    dof="very shallow — calibration marks on one focal plane",
    grade="clinical white — full oxidation spectrum from deep green to copper",
    generic_c=["without atmospheric lighting", "without period setting"],
    specific_c=["no warm fill", "no shadow casting", "no display context"],
    provider="muapi", fallback="muapi", tier=2,
),

"s15": _bp(
    scene_id="s15",
    subject="craftsman hands repairing bronze device",
    setting="Hellenistic workshop bench",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match oil lamp palette from s04; match device patina from s02",
    premise="Knowledge passed forward through the pressure of a craftsman's fingers.",
    shot_type="close_up",
    cam_pos="at bench surface level, hands and device filling the frame",
    subj_decl=(
        "a pair of bare craftsman hands at work on a small bronze geared device "
        "on a dark-stained cedarwood workshop bench, 1st century BCE Hellenistic period"
    ),
    light_src="oil_lamp",
    light_dir="upper left",
    light_temp="warm amber",
    light_feat=(
        "hands and device surface directly in a tight warm cone; "
        "bench edges fall to warm shadow"
    ),
    light_falloff=(
        "the terracotta lamp is the scene's exclusive light source; "
        "beyond one lamp-radius the bench recedes to deep warm shadow"
    ),
    obs1=(
        "bare craftsman hands — calloused at the fingertip pads and palm base, "
        "bronze filing embedded in skin creases, traces of verdigris at the thumbs "
        "from daily device handling"
    ),
    obs2=(
        "small bronze geared device — gear faces showing tool-fresh copper brightness "
        "on working surfaces and green patina in the protected recesses"
    ),
    obs3=(
        "dark-stained cedarwood bench — metal filings scattered across the surface, "
        "a worn groove along the bench edge from repeated tool contact"
    ),
    obs4=None,
    bg=(
        "bench surface falls to warm shadow at the edges; "
        "no background beyond the immediate bench context"
    ),
    ratio="device occupies near-centre of the close-up frame; hands enter from both sides",
    breath="bench shadow expands at all edges beyond the lamp cone",
    cam_verb="static",
    subj_behavior="only the hands move — positioning and applying micro-pressure to the device",
    motion_qual="craftsman applies micro-pressure with one thumb against the gear face, device held steady in the other hand",
    seg0=(
        "hands position a small gear component against the device body, "
        "oil lamp amber resolving tool-fresh copper surface of working faces against the patinated field"
    ),
    seg4=(
        "the craftsman applies micro-pressure with one thumb against the gear face, "
        "device held steady in the other hand, lamp light catching knuckle prominences as warm amber highlights"
    ),
    lens="macro",
    dof="shallow — hands to device surface",
    grade="warm oil lamp amber against deep bench shadow",
    generic_c=["without modern tools", "without contemporary workshop context"],
    specific_c=["no synthetic materials", "no electrical light sources", "no modern workbench surfaces"],
    provider="muapi", fallback="muapi", tier=2,
),

"s16": _bp(
    scene_id="s16",
    subject="ancient bronze gear teeth, uniform precision spacing",
    setting="dark background macro",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match gear patina from s03 — same oxidation zones; this is the definitive precision shot",
    premise="The thing the ancient world made that the modern world took two thousand years to recreate.",
    shot_type="macro",
    cam_pos="at tooth-apex level, register centred horizontally across the frame",
    subj_decl=(
        "ancient bronze gear teeth in a single perfect register across the frame — "
        "their uniform spacing and precisely cut profiles occupying the central horizontal band"
    ),
    light_src="raking_side",
    light_dir="left — very low angle",
    light_temp="warm amber",
    light_feat=(
        "burning along the top edge of each tooth; "
        "tooth-tip highlights near-white amber, driving deep shadow into the valley between each pair"
    ),
    light_falloff=(
        "valley shadows are near-black; "
        "contrast ratio extreme between tooth apex and valley — the geometry made absolute"
    ),
    obs1=(
        "deep green marine patina at the protected tooth bases — "
        "oxidation deepest in the geometry the light cannot reach"
    ),
    obs2=(
        "mid-brown at the tooth mid-face where slight abrasion has thinned the patina layer"
    ),
    obs3=(
        "copper-orange at the tool-cut tooth apices — "
        "where original metal is nearest the surface and the patina layer is thinnest"
    ),
    obs4=None,
    bg="pure unlit black — no gradient, no reflection",
    ratio="tooth register occupies the central horizontal band",
    breath="pure black void above and below the tooth register",
    cam_verb="static",
    subj_behavior="motionless — the raking light makes the case for precision",
    motion_qual="the tooth register holds across the frame without drift — each tooth identical in profile, spacing regular",
    seg0=(
        "tooth register holds across the frame, amber raking light resolving each tooth's "
        "precise geometry from tip to valley, shadow deepening as the light angle establishes"
    ),
    seg4=(
        "composition locks — each tooth identical in profile, spacing regular, "
        "the raking light revealing the full extent of the ancient precision"
    ),
    lens="macro",
    dof="razor-thin — tooth apex plane",
    grade="amber tooth highlights against pure black shadow and void",
    generic_c=["without modern machining context", "without scale reference"],
    specific_c=["no ambient fill", "no background detail", "no contemporary surface visible"],
    provider="muapi", fallback="muapi", tier=2,
),

"s17": _bp(
    scene_id="s17",
    subject="Roman cargo ship deck, amphorae and cargo crates",
    setting="Mediterranean open sea, Roman merchant vessel",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="No specific object continuity — establishes the trade-goods reframing; Mediterranean noon palette",
    premise="Not a unique object. A trade good. Something ordered, invoiced, and shipped.",
    shot_type="wide_establishing",
    cam_pos="elevated at main-deck level, looking aft from the bow",
    subj_decl=(
        "a Roman merchant vessel's main deck spread across the frame — "
        "rough-hewn grey-brown oak planking with iron fittings, "
        "terracotta-red amphorae lashed in rows against the hold covers, "
        "wooden cargo crates roped at the deck's centre"
    ),
    light_src="directional",
    light_dir="overhead — near-vertical noon",
    light_temp="bleached noon white",
    light_feat=(
        "pressing sharp shadows directly below every object on the deck — "
        "amphorae cast short ellipses, mast and rigging throw geometric cross-patterns "
        "of rope-shadow across the oak planking"
    ),
    light_falloff=(
        "Mediterranean sea beyond the hull is flat and brilliant — "
        "noon blue with near-white glare at the surface horizon; no soft zones"
    ),
    obs1=(
        "weathered grey-brown oak deck planking — bleached pale at the surface from salt and sun, "
        "iron deck fittings rust-orange at fixtures"
    ),
    obs2=(
        "terracotta-red amphorae — some corked with pale clay, their surfaces carrying "
        "the salt bloom of open-sea travel, lashed in rows against the hold covers"
    ),
    obs3=(
        "Mediterranean sea beyond the hull — flat and brilliant noon blue "
        "with near-white glare at the surface horizon"
    ),
    obs4=None,
    bg=(
        "deck fills the lower half of frame; amphorae rows and cargo crates occupy the deck centre; "
        "open Mediterranean sea visible above the hull rail at upper frame"
    ),
    ratio="deck cargo occupies the lower two-thirds of frame",
    breath="open sea extends at upper frame; horizon at upper-right",
    cam_verb="slow_pan",
    subj_behavior="deck and cargo are static; the pan traverses the cargo arrangement",
    motion_qual="pan opens at the bow and carries across the cargo arrangement toward the deck centre",
    seg0=(
        "deck planking and cargo arrangement establish — amphorae rows, rope lashing, "
        "noon shadow geometry across oak surface, the bow railing at frame-left"
    ),
    seg4=(
        "cargo crates fill the frame centre, the packed reality of ancient trade in rope "
        "and terracotta revealed from amidships to the stern"
    ),
    lens=None,
    dof="wide — near deck to sea horizon",
    grade="bleached noon, grey oak and terracotta-red against Mediterranean blue",
    generic_c=["without modern shipping materials", "without contemporary cargo"],
    specific_c=["no modern harbour in background", "no anachronistic deck equipment", "no synthetic rope or modern rigging"],
    provider="muapi", fallback="muapi", tier=1,
),

"s18": _bp(
    scene_id="s18",
    subject="multiple craftsmen in Hellenistic workshop",
    setting="large Hellenistic workshop interior",
    period="1st–2nd century BCE",
    dur=8, aspect="16:9",
    continuity="Match oil lamp palette from s04; match production floor scale from s24",
    premise="Not one man. Many. A production floor, not a myth.",
    shot_type="wide",
    cam_pos="at workshop floor level from the far end, three benches in perspective",
    subj_decl=(
        "a large Hellenistic workshop with stone-and-timber construction: "
        "three grey limestone workbenches in a row, each with a craftsman in rough undyed linen "
        "working over a bronze assembly, oil lamps burning at each bench"
    ),
    light_src="oil_lamp",
    light_dir="from each bench surface — multiple overlapping sources",
    light_temp="warm amber (overlapping pools)",
    light_feat=(
        "each bench lit independently in overlapping amber pools; "
        "the intermediate floor between benches in warm dimness"
    ),
    light_falloff=(
        "workshop walls and ceiling recede to deep orange-brown shadow above the lamp radius; "
        "far wall in deep shadow; no external light source"
    ),
    obs1=(
        "grey limestone workbenches — three in a row in perspective, bench surfaces carrying "
        "bronze filings organised by stage of work, tools at consistent positions"
    ),
    obs2=(
        "plain undyed linen chitons on each craftsman — rough workshop cut, "
        "each posture angled toward their individual bench work"
    ),
    obs3=(
        "workshop walls and ceiling in deep orange-brown shadow — "
        "stone-and-timber construction, the architecture receding to darkness above the lamp radius"
    ),
    obs4=None,
    bg=(
        "three benches in perspective from the near end toward the far wall; "
        "three lamp cones overlapping; far wall in deep shadow"
    ),
    ratio="three benches in perspective fill the frame from near to far",
    breath="deep shadow expands at all frame edges beyond the lamp cones",
    cam_verb="static",
    subj_behavior="craftsmen are at work — postures of grinding, fitting, checking",
    motion_qual="the middle craftsman's lamp holds as the closest and brightest source; the workshop depth visible behind to the darkened far wall",
    seg0=(
        "full workshop interior establishes — three benches in perspective, three lamp cones, "
        "three figures, the scale of the production space legible from the architecture"
    ),
    seg4=(
        "middle craftsman's lamp holds as the closest and brightest source, "
        "workshop depth visible behind him to the darkened far wall"
    ),
    lens=None,
    dof="wide — near bench to far wall",
    grade="multiple overlapping amber oil lamp pools against warm-dark workshop interior",
    generic_c=["without modern tools", "without contemporary workshop materials"],
    specific_c=["no electric lighting", "no anachronistic safety equipment", "no non-Hellenistic architectural materials"],
    provider="muapi", fallback="muapi", tier=2,
),

"s19": _bp(
    scene_id="s19",
    subject="Hellenistic Rhodes harbour, ancient port",
    setting="Hellenistic Rhodes harbour",
    period="1st–2nd century BCE",
    dur=8, aspect="16:9",
    continuity="Golden hour palette — match s09; establishes Rhodes as the origin location",
    premise="The city that made the thing.",
    shot_type="wide_establishing",
    cam_pos="at quay level, looking along the quay line toward far end",
    subj_decl=(
        "the Hellenistic harbour of ancient Rhodes with a pale grey limestone quay "
        "running left to right, merchant vessels moored at stone bollards, "
        "their hulls dark-weathered pitch-treated timber with ochre-yellow painted waterlines"
    ),
    light_src="golden_hour",
    light_dir="right — very low angle",
    light_temp="warm amber",
    light_feat=(
        "raking across the quay's limestone surface and drawing every paver joint "
        "and iron mooring ring into sharp relief"
    ),
    light_falloff=(
        "harbour water warm copper-gold at the surface; "
        "deep blue-black in the shadow beneath the hull overhangs; "
        "quay base in deep shadow at the water line"
    ),
    obs1=(
        "pale grey limestone quay — worn smooth along the traffic path, rough at the edges, "
        "iron mooring rings rust-stained with ochre bleeds across the stone"
    ),
    obs2=(
        "dark pitch-treated timber hull planking — upper strakes ochre-yellow or red-brown, "
        "colours absorbing decades of Mediterranean sun and salt"
    ),
    obs3=(
        "harbour water — warm copper-gold at the surface in golden-hour light, "
        "deep blue-black in the shadow beneath the hull overhangs"
    ),
    obs4=None,
    bg=(
        "quay runs across the lower half of the frame; moored vessels fill the middle ground; "
        "harbour water and open sky at upper frame"
    ),
    ratio="quay and vessels fill the lower two-thirds of frame",
    breath="open harbour water extends at upper-left; sky at top",
    cam_verb="slow_pan",
    subj_behavior="quay and vessels are static; the pan follows the quay line",
    motion_qual="pan carries across the quay and reaches the near vessels, hull timbers filling frame-right",
    seg0=(
        "the far quay end establishes in full golden hour light, "
        "limestone and vessel hulls warming simultaneously in the amber"
    ),
    seg4=(
        "near vessel hull timbers fill frame-right, pitch-treated oak and ochre waterlines "
        "in full amber, harbour water copper-gold extending to the left"
    ),
    lens=None,
    dof="wide — quay stone to harbour horizon",
    grade="golden amber hour against dark harbour water and pale limestone",
    generic_c=["without modern harbour infrastructure", "without contemporary vessels"],
    specific_c=["no modern signage", "no anachronistic materials", "no non-Hellenistic vessel forms"],
    provider="muapi", fallback="muapi", tier=2,
),

"s20": _bp(
    scene_id="s20",
    subject="ancient papyrus trade route map, Mediterranean",
    setting="ancient papyrus map surface",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match papyrus palette from s07, s10 — ochre-cream surface, dark ink",
    premise="The mechanism did not travel by accident. Someone planned its route.",
    shot_type="overhead",
    cam_pos="directly above at reading-light level, map filling the frame",
    subj_decl=(
        "an ancient papyrus map of Mediterranean trade routes filling the frame — "
        "the sheet aged ochre-cream, its surface slightly rough with papyrus fibre texture "
        "visible at the margins, ink trade-route lines dark brown against the cream field"
    ),
    light_src="directional",
    light_dir="overhead",
    light_temp="warm amber",
    light_feat=(
        "the full map surface in consistent even illumination — "
        "a practical reading light, not dramatic raking"
    ),
    light_falloff=(
        "even overhead coverage without shadow casting; "
        "the papyrus edges are the frame boundaries"
    ),
    obs1=(
        "aged ochre-cream papyrus — slightly rough with papyrus fibre texture, "
        "faint cracking along horizontal fibre lines from repeated handling"
    ),
    obs2=(
        "dark brown ink trade-route lines — thick connecting lines between coastal points, "
        "fine Greek script annotations at the port nodes"
    ),
    obs3=(
        "water-stain shadow along the lower margin — where moisture once touched the surface, "
        "a pale irregular zone in the cream field"
    ),
    obs4=None,
    bg=(
        "the papyrus map fills the frame edge to edge; "
        "overhead light covers the surface evenly; no objects beyond the map"
    ),
    ratio="map fills the full frame",
    breath="no breathing room — map edge to frame edge",
    cam_verb="static",
    subj_behavior="motionless",
    motion_qual="the map surface holds at rest — the route network the sole content",
    seg0=(
        "map surface establishes in warm overhead light, the full route network visible "
        "from the overhead perspective, ink markings resolving from the central "
        "Mediterranean line outward"
    ),
    seg4=(
        "overhead light holds even, papyrus texture and ink line quality visible at rest — "
        "the route from Rhodes to Rome present in the visual field"
    ),
    lens="overhead",
    dof="even across the flat papyrus surface",
    grade="warm amber overhead against ochre-cream papyrus and dark brown ink",
    generic_c=["without modern maps or projections", "without contemporary notation"],
    specific_c=["no cast shadows from overhead vantage", "no modern materials beneath the papyrus", "no anachronistic cartographic conventions"],
    provider="muapi", fallback="muapi", tier=2,
),

"s21": _bp(
    scene_id="s21",
    subject="ancient scholar studying gear train diagrams",
    setting="Hellenistic library, papyrus scrolls",
    period="1st–2nd century BCE",
    dur=8, aspect="16:9",
    continuity="Match Hellenistic library palette from s07; papyrus from s20",
    premise="The theory that made the instrument possible, worked out in a room that no longer exists.",
    shot_type="medium",
    cam_pos="at library floor level, scholar at composition centre with diagram sheet spread before him",
    subj_decl=(
        "an ancient Greek scholar in a white linen chiton standing at a dark cedarwood reading stand "
        "in a Hellenistic library, a large papyrus sheet spread flat before him carrying "
        "geometric gear diagrams in ink — circles, epicycle arcs, angular divisions"
    ),
    light_src="diffuse",
    light_dir="from column openings at far end",
    light_temp="warm amber",
    light_feat=(
        "left side of the chiton and the upper papyrus surface in broad gentle amber shafts; "
        "column-light shafts visible behind the figure as warm amber verticals"
    ),
    light_falloff=(
        "reading stand and right side of chiton in cooler shadow; "
        "library shelves at the perimeter hold warm dimness"
    ),
    obs1=(
        "white linen chiton — lighter than wool, the fabric holding warm amber daylight "
        "at the near shoulder while the far side cools toward shadow"
    ),
    obs2=(
        "large papyrus sheet — the scholar's full arm-width, carrying interlocking circular "
        "gear-train geometry in dark ink: epicycle arcs, angular divisions, numerical annotations"
    ),
    obs3=(
        "pale marble columns at the far end — column-light shafts visible as warm amber "
        "verticals framing the scholar in the background"
    ),
    obs4=None,
    bg=(
        "reading stand at composition centre; papyrus sheet spread flat on the stand; "
        "column-light shafts at the far end framing the figure; library shelves in peripheral shadow"
    ),
    ratio="scholar and diagram occupy the central half of frame",
    breath="column-light shafts frame both sides; library depth extends behind",
    cam_verb="static",
    subj_behavior="scholar's hands rest flat on the table at either side of the sheet, not touching it",
    motion_qual="the scholar's gaze holds on the diagram, the library still around him",
    seg0=(
        "scholar and diagram establish — papyrus gear geometry resolving in warm daylight, "
        "column-light shafts visible behind as warm amber verticals"
    ),
    seg4=(
        "scholar's gaze holds on the diagram, library still around him; "
        "gear diagrams remain the visual centre of the composition"
    ),
    lens=None,
    dof="medium-shallow — scholar and diagram to column background",
    grade="warm diffuse daylight amber against pale marble and cream papyrus",
    generic_c=["without modern materials", "without contemporary notation"],
    specific_c=["no artificial fill", "no direct-to-camera gaze", "no non-Hellenistic library context"],
    provider="muapi", fallback="muapi", tier=2,
),

"s22": _bp(
    scene_id="s22",
    subject="single human hand holding bronze gear fragment",
    setting="dark neutral surface, single spotlight",
    period="timeless",
    dur=8, aspect="16:9",
    continuity="Match fragment patina from s02 — green-brown marine, copper-orange at edges, calcite deposits",
    premise="Two thousand years reduced to the weight of an object in a single hand.",
    shot_type="close_up",
    cam_pos="at hand level, hand and fragment filling the spotlight cone",
    subj_decl=(
        "a single bare human hand holding a corroded bronze gear fragment in an open palm "
        "against a dark matte surface"
    ),
    light_src="spotlight_overhead",
    light_dir="directly above",
    light_temp="warm amber",
    light_feat=(
        "hand and fragment within the spotlight radius — "
        "the skin-to-metal proximity intimate and immediate in the warm cone"
    ),
    light_falloff=(
        "everything beyond the immediate spotlight radius falls to pure darkness; "
        "dark matte surface beneath absorbs all reflected light"
    ),
    obs1=(
        "bare adult hand — calloused at the fingertip pads and base of palm, "
        "fine skin creasing at the knuckles, bronze filing embedded in the skin creases"
    ),
    obs2=(
        "corroded bronze gear fragment in the palm — dense green-brown marine patina, "
        "pale cream calcite deposits at interior angles, copper-orange at fractured edges"
    ),
    obs3=(
        "dark matte surface beneath the hand — "
        "absorbing all reflected light, zero surface detail"
    ),
    obs4=None,
    bg="pure unlit darkness beyond the spotlight cone; no surface detail",
    ratio="hand and fragment occupy the spotlight cone at frame centre",
    breath="pure darkness expands at all frame edges",
    cam_verb="static",
    subj_behavior="the hand holds without motion — fingers curled slightly but not gripping",
    motion_qual="the fragment rests in the open palm; the spotlight warmth consistent throughout",
    seg0=(
        "hand establishes in the spotlight, fragment's patina surface resolving in warm amber, "
        "the skin-to-metal proximity intimate and immediate"
    ),
    seg4=(
        "the hand holds without motion — fingers curled slightly but not gripping, "
        "fragment resting in open palm, spotlight warmth consistent"
    ),
    lens="macro",
    dof="shallow — hand surface to fragment face",
    grade="warm amber spotlight against deep shadow, skin tones and bronze patina",
    generic_c=["without modern context", "without institutional display"],
    specific_c=["no background detail", "no motion in the hand", "no contemporary objects in frame"],
    provider="muapi", fallback="muapi", tier=2,
),

"s23": _bp(
    scene_id="s23",
    subject="empty ancient workshop bench with abandoned tools",
    setting="abandoned Hellenistic workshop",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match workshop palette from s04, s18 — grey limestone bench; absence of the oil lamp distinguishes this scene",
    premise="The question that outlasts everyone who might have answered it.",
    shot_type="wide",
    cam_pos="at bench height several metres back, bench filling lower half of frame",
    subj_decl=(
        "an empty grey limestone workbench in an abandoned Hellenistic workshop, "
        "its surface bare except for scattered bronze filings and two iron tools "
        "left at an angle suggesting mid-work interruption rather than end-of-day storage"
    ),
    light_src="directional",
    light_dir="left — high window, late afternoon",
    light_temp="warm amber (fading)",
    light_feat=(
        "the tools at their last position catching scattered warm light as gold points; "
        "bronze filings on the bench surface catching the low warm light"
    ),
    light_falloff=(
        "fading quality — more presence of warmth than intensity; "
        "workshop walls recede to cool shadow at the perimeter; "
        "the high window slot at left provides the sole light entry"
    ),
    obs1=(
        "grey limestone bench surface — worn smooth from decades of work, "
        "tool-contact zones polished to a faint sheen while the edges remain rough"
    ),
    obs2=(
        "two iron tools left mid-work — a graver and a dividing tool, "
        "iron with worn wooden handles, positioned at an angle suggesting interruption not storage"
    ),
    obs3=(
        "fine dust settled across the bench surface and workshop floor — "
        "a pale grey layer confirming extended absence without dramatic disturbance"
    ),
    obs4=None,
    bg=(
        "workshop walls recede to shadow at the perimeter; "
        "the high window slot at upper-left is the sole light source; "
        "no other objects visible in the frame"
    ),
    ratio="bench occupies the lower half of frame; workshop shadow fills the upper half",
    breath="workshop shadow expands above the bench and at both sides",
    cam_verb="static",
    subj_behavior="motionless — nothing disturbs the dust",
    motion_qual="the light holds at its low angle; the bench remaining still; the two tools in their mid-work position",
    seg0=(
        "empty bench establishes in fading amber window light — tools visible at last position, "
        "bronze filings catching low warm light as scattered gold points, dust surface confirming silence"
    ),
    seg4=(
        "light holds at its low angle, bench remaining still, two tools in their mid-work position, "
        "the question of what stopped the work unresolved"
    ),
    lens=None,
    dof="wide — near bench to workshop wall",
    grade="fading amber window light against cool grey stone and deep workshop shadow",
    generic_c=["without modern context", "without contemporary objects"],
    specific_c=["no human presence", "no artificial fill", "no signs of dramatic disturbance"],
    provider="muapi", fallback="muapi", tier=2,
),

"s24": _bp(
    scene_id="s24",
    subject="multiple bronze devices on Rhodian workshop shelf",
    setting="Rhodian workshop interior",
    period="1st–2nd century BCE",
    dur=8, aspect="16:9",
    continuity="Match oil lamp palette from s04; match device patina from s11 — green-brown with tool-fresh copper faces",
    premise="Not a rarity. A product line.",
    shot_type="medium",
    cam_pos="at shelf height, devices arranged in a row across the frame",
    subj_decl=(
        "multiple small bronze geared astronomical devices arranged on a smoke-dark "
        "cedarwood workshop shelf in a Rhodian workshop interior, 1st-2nd century BCE"
    ),
    light_src="oil_lamp",
    light_dir="near-left end of shelf",
    light_temp="warm amber (falling off across device row)",
    light_feat=(
        "the nearest device fully lit in warm amber; "
        "devices further down the shelf receiving progressively less light; "
        "last device near the right edge held only in warm dimness"
    ),
    light_falloff=(
        "deep workshop shadow beyond the shelf; "
        "the lamp creates a warm gradient across the device row from left (full amber) to right (warm dimness)"
    ),
    obs1=(
        "smoke-dark cedarwood shelf — dark from lamp smoke over years of use, "
        "surface carrying impressions of previous objects as rings and rectangles in the dust"
    ),
    obs2=(
        "multiple bronze geared devices in a row — each the span of two hands, "
        "green-brown patina on protected surfaces, tool-fresh copper brightness on working faces"
    ),
    obs3=(
        "deep workshop shadow beyond the shelf — the workshop receding to darkness behind, "
        "shelving depth visible as warm dimness"
    ),
    obs4=None,
    bg=(
        "the shelf spans the frame; devices in a row from near-left to far-right; "
        "oil lamp at the left end creating a gradient from full amber to warm dimness"
    ),
    ratio="the shelf and device row fill the full frame width",
    breath="deep workshop shadow opens above the shelf",
    cam_verb="static",
    subj_behavior="devices motionless on the shelf — their number and uniformity the sole narrative function",
    motion_qual="the lamp flame holds; devices motionless on the shelf, their number and uniformity the sole content",
    seg0=(
        "full shelf establishes, oil lamp amber falling from left to right, device row resolving "
        "from nearest (fully lit) to furthest (warm dimness), workshop depth visible beyond"
    ),
    seg4=(
        "lamp flame holds, devices motionless on the shelf, their number and uniformity — "
        "the fact of multiple copies — as their sole narrative function"
    ),
    lens=None,
    dof="shallow-medium — near device sharp, far devices in progressive soft focus",
    grade="warm oil lamp amber falling off across device row, deep workshop shadow beyond",
    generic_c=["without modern context", "without contemporary materials"],
    specific_c=["no electric lighting", "no labels or signage", "no anachronistic workshop objects"],
    provider="muapi", fallback="muapi", tier=2,
),

"s25": _bp(
    scene_id="s25",
    subject="mechanism fragments on coastal seabed",
    setting="rocky coastal seabed approximation",
    period="contemporary — underwater context",
    dur=8, aspect="16:9",
    continuity="Match fragment patina from s02 — but with marine concretion layer on top; cold blue-green palette",
    premise="The objects that waited.",
    shot_type="close_up",
    cam_pos="at fragment surface level, fragments filling the frame",
    subj_decl=(
        "corroded bronze fragments at rest on water-smoothed pale limestone "
        "in a diffuse blue-green light environment suggesting the underwater coastal world"
    ),
    light_src="diffuse",
    light_dir="from above — scattered",
    light_temp="cold blue-green",
    light_feat=(
        "scattered directionless cold blue-green — the way deep coastal water scatters "
        "Mediterranean sunlight; no single source, no directed shadow"
    ),
    light_falloff=(
        "even cold scatter across the fragment surfaces; "
        "no warm zone anywhere in the frame"
    ),
    obs1=(
        "thick marine concretions covering the majority of each fragment exterior — "
        "calcium-grey, rough, the accretion of centuries of seawater"
    ),
    obs2=(
        "green-brown original bronze patina visible only at points where the concretion "
        "has broken away — the colour of the thing beneath the crust"
    ),
    obs3=(
        "water-smoothed pale limestone surface beneath the fragments — covered with "
        "pale grey coralline algae deposits, fine sediment settled into every horizontal crevice"
    ),
    obs4=None,
    bg=(
        "water-smoothed limestone extends around the fragments; "
        "cold blue-green scatter fills the frame; no warm zone"
    ),
    ratio="fragments occupy the central two-thirds of the close-up frame",
    breath="limestone surface extends at all frame edges",
    cam_verb="static",
    subj_behavior="motionless — the same stillness maintained for two thousand years",
    motion_qual="no movement, no water motion — the stillness is the whole content",
    seg0=(
        "fragments establish in diffuse blue-green light, marine concretion surface resolving "
        "as a pale grey-calcium crust, underlying bronze patina visible at worn points"
    ),
    seg4=(
        "light holds in its scattered cold quality, fragments absolutely still — "
        "the same stillness they maintained for two thousand years"
    ),
    lens="macro",
    dof="shallow — fragment surfaces across the rock plane",
    grade="cold scattered blue-green, calcium-grey marine concretion, bronze patina reveal",
    generic_c=["without modern surface context", "without artificial fill light"],
    specific_c=["no warm colour sources", "no motion in water or sediment", "no modern objects on the seabed surface"],
    provider="muapi", fallback="muapi", tier=2,
),

"s26": _bp(
    scene_id="s26",
    subject="ancient precision tools on workshop bench",
    setting="Hellenistic workshop bench",
    period="1st–2nd century BCE",
    dur=8, aspect="16:9",
    continuity="Match workshop bench palette from s04, s23 — grey limestone bench; match oil lamp palette from s04",
    premise="The instruments that made the instrument.",
    shot_type="close_up",
    cam_pos="at bench surface level slightly elevated, tools arranged across the frame",
    subj_decl=(
        "ancient iron precision tools arranged on a grey limestone workshop bench surface — "
        "a dividing tool, a graver with a pointed bronze tip, "
        "a wooden layout compass with iron points"
    ),
    light_src="oil_lamp",
    light_dir="lower left — very low oblique angle",
    light_temp="warm amber (raking)",
    light_feat=(
        "catching the iron tool shafts along their length as sharp amber lines; "
        "the dividing tool's precision tip catching lamp warmth as the brightest amber point"
    ),
    light_falloff=(
        "bench surface in deep shadow behind each tool; "
        "rapid falloff to the right — the low raking source creates long amber shaft highlights "
        "and deep bench shadow"
    ),
    obs1=(
        "iron dividing tool shaft — dark from oxidation at unexposed areas, "
        "polished bright at the grip zone where the craftsman's hand has kept the metal clean"
    ),
    obs2=(
        "wooden handle grips — dark from hand oil and warmth, worn smooth at the grip points, "
        "some fibre splitting visible at the end-grain"
    ),
    obs3=(
        "grey limestone bench surface — fine brass and iron filings embedded in the stone pores, "
        "scratch marks from previous work sessions, the lower-left lamp raking the surface into high relief"
    ),
    obs4=None,
    bg=(
        "the tools are arranged in working positions across the bench; "
        "raking light creates long amber shadows to the right; bench edges fall to shadow"
    ),
    ratio="tools fill the frame width in a close arrangement",
    breath="deep bench shadow opens at the right and upper edges",
    cam_verb="static",
    subj_behavior="tools motionless in their working positions",
    motion_qual="the dividing tool's precision tip catches lamp warmth as the brightest amber point — closest to the viewer",
    seg0=(
        "tool arrangement establishes in raking lamp light, each shaft catching amber along its length, "
        "bench surface coming forward in the low raking angle"
    ),
    seg4=(
        "lamp light holds, tools motionless in working positions — dividing tool's precision tip "
        "closest to the viewer, catching lamp warmth as a bright amber point"
    ),
    lens="macro",
    dof="shallow-medium — tool tips to handle ends",
    grade="warm oil lamp amber raking across iron and grey stone",
    generic_c=["without modern tools", "without contemporary workshop context"],
    specific_c=["no synthetic materials", "no electric light sources", "no non-ancient tool forms"],
    provider="muapi", fallback="muapi", tier=2,
),

"s27": _bp(
    scene_id="s27",
    subject="abandoned workshop interior, three empty benches, open doorway",
    setting="Rhodian workshop, indeterminate end-state",
    period="1st century BCE",
    dur=8, aspect="16:9",
    continuity="Match bench palette from s23 — grey limestone, bronze filings; the open doorway is the defining visual contrast",
    premise="A workshop that stopped without announcement.",
    shot_type="wide",
    cam_pos="at workshop floor level from the near end, three benches in perspective toward open doorway",
    subj_decl=(
        "three empty grey stone workbenches standing in a row across a Hellenistic workshop interior, "
        "their surfaces bare, tools removed, bronze filings and fine metal dust still on the ground, "
        "a doorway at the far wall open to bright Aegean daylight"
    ),
    light_src="directional",
    light_dir="from far doorway and high window slit at left",
    light_temp="warm white (Aegean daylight at doorway) against cool interior dimness",
    light_feat=(
        "the doorway a sharp-edged rectangle of warm white light — "
        "the scene's single bright reference against the workshop's interior dimness"
    ),
    light_falloff=(
        "interior light is cool and dim — only the doorway and a high window slit at left provide entry; "
        "no lamp burning, no fire; the benches are lit only by reflected ambient"
    ),
    obs1=(
        "three grey stone workbenches in a row — worn at the working centre, rough at the edges, "
        "bronze filings embedded in the stone pores, graver scratch pattern on the central bench face"
    ),
    obs2=(
        "rough limestone plaster walls — pale grey, with the shadows of removed tool hooks "
        "visible as slightly lighter patches where hooks blocked the wall from smoke-darkening"
    ),
    obs3=(
        "ground debris — bronze filings and fine metal dust on the floor below where feet no longer stand, "
        "confirming use history without dramatic exit"
    ),
    obs4=None,
    bg=(
        "three benches in perspective from near to far; "
        "open doorway at the far wall as the single bright reference; "
        "interior light from doorway and high window slit only"
    ),
    ratio="three benches in perspective fill the frame from near to far",
    breath="doorway light opens at the far end; workshop shadow at both sides",
    cam_verb="static",
    subj_behavior="motionless — the workshop is simply no longer in use",
    motion_qual="the absence is complete and undramatic — open doorway, empty benches, ground filings",
    seg0=(
        "three empty benches establish in interior dimness, doorway light holding as "
        "the scene's single bright reference, faint evidence of work history visible "
        "in bench surfaces and ground debris"
    ),
    seg4=(
        "scene holds without motion — open doorway, empty benches, ground filings; "
        "the absence complete and undramatic"
    ),
    lens=None,
    dof="wide — near bench to open doorway",
    grade="warm Aegean daylight at doorway against cool workshop interior dimness, grey stone throughout",
    generic_c=["without modern context", "without destruction or drama"],
    specific_c=["no human figures", "no contemporary objects", "no signs of violence or fire"],
    provider="muapi", fallback="muapi", tier=2,
),

"s28": _bp(
    scene_id="s28",
    subject="Stais recognising gear teeth in bronze fragment",
    setting="Athens museum storeroom, 1902",
    period="1902 CE",
    dur=8, aspect="16:9",
    continuity="Match s05 gaslit storeroom palette; this is the recognition follow-up — the lean is closer",
    premise="The second look. The one that changes everything.",
    shot_type="medium_close_up",
    cam_pos="at storeroom shelf level, archaeologist at frame centre-left",
    subj_decl=(
        "a Greek archaeologist in a heavy dark wool suit leaning close to a corroded bronze fragment "
        "on a wooden shelf in an Athens museum storeroom, 1902, angling his gaze "
        "to bring the gear tooth edge into the dim amber gaslight"
    ),
    light_src="gaslight",
    light_dir="upper wall bracket",
    light_temp="warm amber (dim)",
    light_feat=(
        "the near side of the archaeologist's face in warm amber; "
        "the bronze fragment's gear teeth picking up the low amber at their leading edges "
        "while the valleys fall to darkness"
    ),
    light_falloff=(
        "the storeroom shelf behind carries artefacts in near-darkness — "
        "shapes legible as boxes and bundles but without detail; "
        "no secondary fill"
    ),
    obs1=(
        "heavy dark wool suit — woven close, absorbing the gaslight warmth at the near lapel only, "
        "receding to near-black on the far side from the light"
    ),
    obs2=(
        "corroded bronze fragment held in bare fingers — green-brown patina visible in amber warmth, "
        "gear tooth leading edge picking up the low gaslight"
    ),
    obs3=(
        "storeroom shelf carrying other artefacts in near-darkness — "
        "shapes legible as boxes and bundles, forming an unlit depth behind the archaeologist"
    ),
    obs4=None,
    bg=(
        "the archaeologist occupies the left-centre of the medium close frame; "
        "the bronze fragment is the focal object between his face and the gaslight; "
        "storeroom shelves form the dark background"
    ),
    ratio="archaeologist and fragment occupy the central two-thirds of frame",
    breath="storeroom shadow opens at both sides and below",
    cam_verb="static",
    subj_behavior="the lean is held — the moment of recognition approaching without announcement",
    motion_qual="the archaeologist's head angle tightens in the second half — the lean toward the gear tooth edge fractionally deepening",
    seg0=(
        "archaeologist's profile and bronze fragment establish in gaslight, "
        "gear tooth edge resolving in low amber warmth, storeroom darkness framing the composition"
    ),
    seg4=(
        "archaeologist's head angle tightens — the lean toward gear tooth edge fractionally deepening, "
        "the moment of recognition approaching without announcement"
    ),
    lens=None,
    dof="shallow — archaeologist's near eye to bronze fragment surface",
    grade="soft dim amber gaslight against deep storeroom shadow, dark wool and green-brown bronze",
    generic_c=["without modern lighting", "without electric fixtures"],
    specific_c=["no clinical brightness", "no contemporary objects", "no post-1902 materials"],
    provider="muapi", fallback="muapi", tier=2,
),

"s29": _bp(
    scene_id="s29",
    subject="long dark stone corridor, single distant light",
    setting="dark stone corridor",
    period="timeless",
    dur=8, aspect="16:9",
    continuity="No material continuity — pure spatial/atmospheric; the single amber point is the only warm element",
    premise="Two thousand years of absence, rendered as architecture.",
    shot_type="wide",
    cam_pos="at corridor floor level from one end, looking toward the far vanishing point",
    subj_decl=(
        "a long rough-cut stone block corridor extending from the foreground deep into the frame's distance, "
        "in near-total darkness, with a single amber light point at the vanishing end"
    ),
    light_src="directional",
    light_dir="from far end — single point",
    light_temp="warm amber (single point, minimal)",
    light_feat=(
        "barely illuminating the near foreground stone blocks via faint reflected ambient; "
        "the single amber point holds warmth without brightness at the vanishing end"
    ),
    light_falloff=(
        "near two-thirds of the corridor in near-total darkness; "
        "middle section pure black — no surface detail recoverable; "
        "no secondary light source of any kind"
    ),
    obs1=(
        "rough-cut stone block walls — barely legible in near foreground via faint reflected ambient, "
        "block joints visible at the near edge only"
    ),
    obs2=(
        "stone corridor floor — the same material as walls, worn smooth at the traffic centre, "
        "falling to pure black in the middle section where no reflected light reaches"
    ),
    obs3=(
        "single amber light point at the vanishing end — "
        "indeterminate in source, small, holding warmth without brightness"
    ),
    obs4=None,
    bg=(
        "corridor extends from foreground to vanishing point; "
        "near two-thirds in near-total darkness; "
        "no branching, no doorways, no furnishing"
    ),
    ratio="corridor fills the full frame, narrowing toward the vanishing point",
    breath="pure black expands above and at both sides in the mid-section",
    cam_verb="static",
    subj_behavior="motionless — the corridor holds its silence",
    motion_qual="the darkness between the viewer and the light resolves as total; the corridor holds its silence without change",
    seg0=(
        "composition establishes — foreground stone blocks barely legible, corridor darkness "
        "deepening to full black in the middle section, single distant amber point at the vanishing end"
    ),
    seg4=(
        "single distant light holds without change — darkness between viewer and light resolving "
        "as total; corridor holds its silence"
    ),
    lens=None,
    dof="wide — foreground stone to distant light point",
    grade="near-total black with single amber point at depth",
    generic_c=["without human figures", "without decorative detail"],
    specific_c=["no intermediate light sources", "no door or window openings", "no contemporary materials"],
    provider="muapi", fallback="muapi", tier=2,
),

"s30": _bp(
    scene_id="s30",
    subject="single corroded bronze gear fragment",
    setting="isolated dark void",
    period="timeless — object without context",
    dur=8, aspect="16:9",
    continuity="Match mechanism patina from s02 — green-brown marine patina, copper-orange at worn edges",
    premise="A relic of impossible precision suspended at the edge of recorded history.",
    shot_type="extreme_close_up",
    cam_pos="locked at subject level, fragment at left-third of frame",
    subj_decl=(
        "a single corroded bronze gear fragment with dense green-brown marine patina "
        "and copper-orange worn edges occupies the left third of a 16:9 frame "
        "against an absolute dark void"
    ),
    light_src="directional",
    light_dir="upper left",
    light_temp="warm amber",
    light_feat=(
        "corroded ridges and gear tooth faces, casting micro-shadow relief "
        "into the tooth valleys at a low oblique angle"
    ),
    light_falloff=(
        "opposing face recedes into deep surrounding shadow with no ambient fill; "
        "hard falloff to pure unlit black"
    ),
    obs1=(
        "green-brown marine patina at depth — layered oxidation built over two millennia "
        "of seawater immersion, colour deepest in the protected tooth valleys"
    ),
    obs2=(
        "copper-orange exposed at worn tooth edges where original bronze alloy is nearest "
        "the surface and patina layer has thinned from historical contact"
    ),
    obs3=(
        "fine radial cracks radiating from the central bore — stress fractures in the "
        "alloy visible as darker lines against the patina field"
    ),
    obs4=(
        "microscopic pit corrosion across the broad face — a matte, light-absorbing "
        "surface texture that deepens shadow rather than reflecting the amber source"
    ),
    bg=(
        "absolute unlit black void — no surface, no gradient falloff, no depth haze; "
        "pure negative space with zero reflected light"
    ),
    ratio="one-third of frame width",
    breath="below the fragment and to the right",
    cam_verb="static",
    subj_behavior="motionless",
    motion_qual=(
        "the only motion is a slow thermal shift in light temperature as it "
        "diffuses across the patina — no mechanical movement, no oscillation"
    ),
    seg0=(
        "fragment surfaces from absolute darkness as light resolves detail across "
        "the corroded face, patina micro-texture sharpening progressively from the "
        "brightest lit gear tooth inward toward the shadow"
    ),
    seg4=(
        "light temperature deepens from amber to copper-orange, shadow edges harden "
        "along gear teeth, void consolidates to pure negative space behind the fragment"
    ),
    lens="macro",
    dof="shallow — gear teeth to patina face on one focal plane",
    grade="warm amber-copper against pure black",
    generic_c=["without modern surfaces", "without institutional display context"],
    specific_c=["no ambient fill", "no background detail", "no lens flare", "no motion blur", "no museum case or display hardware", "no label or scale reference"],
    provider="muapi", fallback="muapi", tier=2,
),

"s31": _bp(
    scene_id="s31",
    subject="bronze gear fragment on cliff ledge, Aegean horizon",
    setting="Greek island cliff edge, open sea",
    period="timeless",
    dur=8, aspect="16:9",
    continuity="Match fragment patina from s02; match golden hour palette from s09 — closing visual rhyme",
    premise="An object that held its question for two thousand years, pointed at the sea that kept it.",
    shot_type="wide",
    cam_pos="at cliff-ledge level slightly elevated, fragment at lower-left foreground",
    subj_decl=(
        "a corroded bronze gear fragment resting on a pale limestone ledge at a Greek island cliff edge, "
        "the fragment in the lower-left foreground while the vast Aegean Sea fills the right "
        "two-thirds of the frame and the full background, the horizon at mid-frame"
    ),
    light_src="golden_hour",
    light_dir="right — near-flat angle",
    light_temp="warm copper-amber",
    light_feat=(
        "raking across the limestone ledge surface and the fragment simultaneously — "
        "the stone catching warmth as pale amber-cream, "
        "the bronze fragment's patinated face burning in deep copper-amber at its lit surfaces"
    ),
    light_falloff=(
        "fragment recesses fall to rich shadow; "
        "Aegean Sea deepens from copper-gold at the illuminated surface to cerulean in the shadow areas; "
        "horizon line sharp"
    ),
    obs1=(
        "corroded bronze gear fragment — patinated face burning in deep copper-amber at its lit surfaces, "
        "falling to rich shadow in the recesses; gear teeth profile visible in silhouette against the sea"
    ),
    obs2=(
        "pale limestone ledge — catching golden-hour warmth as pale amber-cream, "
        "fractured at the cliff edge with iron-oxide streaking along the fracture faces"
    ),
    obs3=(
        "Aegean Sea — warm copper-gold at the illuminated surface, "
        "deepening to cerulean in the shadow areas, horizon line sharp"
    ),
    obs4=None,
    bg=(
        "Aegean Sea fills the right two-thirds of frame and the full background; "
        "horizon at mid-frame; no coastal development visible"
    ),
    ratio="gear fragment occupies lower-left foreground as a small silhouette",
    breath="Aegean expands to the right and upper frame",
    cam_verb="slow_pan",
    subj_behavior="fragment is static on the ledge; the pan moves rightward toward the horizon",
    motion_qual="the fragment recedes toward the left frame edge as the full Aegean horizon opens across the frame — the question and the sea together",
    seg0=(
        "the fragment holds in close view, its patina surface in full golden amber, "
        "the sea establishing depth behind it"
    ),
    seg4=(
        "the fragment recedes toward the left frame edge as the full Aegean horizon "
        "opens across the frame, the question and the sea together"
    ),
    lens=None,
    dof="shallow foreground to infinity background",
    grade="warm golden amber against cerulean-copper Aegean",
    generic_c=["without modern coastal development", "without contemporary objects"],
    specific_c=["no human figures", "no artificial light sources", "no non-Mediterranean coastal vegetation"],
    provider="muapi", fallback="muapi", tier=1,
),

}  # end BLUEPRINTS


def generate(scene_id: str) -> dict:
    scene = load_scene(scene_id)
    if scene_id not in BLUEPRINTS:
        raise ValueError(f"No blueprint definition for scene {scene_id!r}")
    bp = BLUEPRINTS[scene_id].copy()
    bp["_source_visual_spec"] = scene["visual_spec"]
    bp["_source_production_spec"] = scene["production_spec"]
    return bp


def main():
    parser = argparse.ArgumentParser(description="Generate cinematic blueprint for a scene")
    parser.add_argument("scene_id", help="Scene ID, e.g. s30")
    parser.add_argument("--output", help="Output path for blueprint JSON")
    args = parser.parse_args()

    bp = generate(args.scene_id)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(bp, f, indent=2)
        print(f"Blueprint written: {out}")
    else:
        print(json.dumps(bp, indent=2))


if __name__ == "__main__":
    main()
