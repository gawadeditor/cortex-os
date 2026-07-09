"""
Experiment: VPD Cinematic Prompt Compare v1
Applies cinematic prompt architecture (vpd_cinematic_prompt_skill_v1) to all
31 main episode scenes and writes BEFORE/AFTER comparisons.

Hard guardrails:
  - No pipeline import
  - No MUAPI or provider calls
  - No thumbnail generation
  - Does not modify visual_package_ep_000.json
  - Writes only inside .hypogeum-assets/experiments/vpd_cinematic_prompt_compare_v1/
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VISUAL_PACKAGE = (
    REPO_ROOT / ".hypogeum-assets" / "episodes" / "ep_000" / "visual_package_ep_000.json"
)
OUTPUT_DIR = REPO_ROOT / ".hypogeum-assets" / "experiments" / "vpd_cinematic_prompt_compare_v1"
BLUEPRINT = "vpd_cinematic_prompt_skill_v1"

# ── AFTER prompts — hand-authored per cinematic architecture spec ──────────────

AFTER = {

"s01": {
"primary": (
    "A body breaks the surface of the Aegean for the last time before the world changes. "
    "Wide establishing shot, a sponge diver in rough water-darkened canvas gear erupts "
    "through the wave surface against the pale limestone Antikythera coastline, seawater "
    "cascading from hemp rope and saturated fabric. Bright Mediterranean noon sun descends "
    "at near-perpendicular angle, bleaching wave crests to white and driving hard reflections "
    "across the fractured surface; the cliff face at frame-right holds warmth in its "
    "iron-streaked limestone, shadow gathering only in deep fissures. The canvas shirt carries "
    "layers of sea service — salt-bleached along the shoulders, charcoal-heavy where seawater "
    "saturates; the hemp rope at the diver's waist beaded with water. Aegean fills three-quarters "
    "of the frame in intense ultramarine that deepens below the scatter layer; the cliff edge "
    "anchors the right side, open horizon at upper-left. Camera: slow pan right. "
    "0-4s: diver's torso clears the water surface, canvas gear shedding sheets of seawater, "
    "wave disturbance radiating outward from the point of emergence. "
    "4-8s: pan reaches toward the open ultramarine horizon as the disturbed surface settles, "
    "diver receding to frame-left edge. "
    "[4K] [24fps] [slow pan] [aspect ratio: 16:9] [depth of field: wide, surface to horizon] "
    "[color grade: bleached noon white against ultramarine blue] "
    "[without modern watercraft] [without contemporary diving equipment] "
    "[no wetsuit] [no modern coastline infrastructure]"
),
"fallback": (
    "Sponge diver in canvas gear surfacing from Aegean, pale limestone coastline, "
    "bleached noon sun, ultramarine water, wide shot, without modern equipment."
),
},

"s02": {
"primary": (
    "The moment a two-thousand-year-old machine introduces itself. "
    "Extreme close-up, bronze gear fragments arranged on a stone examination table in 1902 Athens, "
    "each piece carrying dense green-brown marine patina with copper-orange exposed at sheared edges "
    "where ancient alloy fractured clean. A single warm amber lamp source enters from the upper left, "
    "raking at a low oblique angle across corroded tooth faces and casting micro-shadow into every "
    "pit and fissure; the surrounding table surface recedes into warm shadow beyond the lamp's radius. "
    "The fragments carry distinct surface histories — heavy concentric sea-oxidation layering, lighter "
    "iron-red intrusions at the bronze grain boundaries, and a fine calcite crust that catches the "
    "lamp's warmth as pale cream against the dark bronze field. The stone table is grey-beige marble, "
    "stained with iron bleed-out where fragments rested during recovery. Camera: static, locked. "
    "0-4s: the central fragment fills the lower half of frame as lamp light resolves gear tooth "
    "surface texture, patina detail sharpening from the brightest lit face inward to shadow. "
    "4-8s: light stabilises at full amber warmth, shadow edges of each tooth deepening as the "
    "eye adjusts to the micro-scale precision. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: shallow, fragment surface to stone table] "
    "[color grade: warm amber against cool grey marble] "
    "[without modern display labelling] [without institutional context] "
    "[no fluorescent fill] [no clinical lighting]"
),
"fallback": (
    "Corroded bronze gear fragments on grey marble table, warm amber lamp from upper left, "
    "green-brown marine patina, extreme close-up, without institutional display context."
),
},

"s03": {
"primary": (
    "Precision this exact should not exist. "
    "Macro close-up, ancient bronze interlocking gear teeth meshing in absolute contact, "
    "each tooth face carrying green-brown marine patina with bright copper-orange at the apex "
    "of the engagement point where metal meets metal. Amber raking side light enters from the "
    "left at a low angle, burning hard along the leading edge of each tooth and throwing the "
    "valley between teeth into dense shadow; the contact point between the two meshing gears "
    "holds a micro-highlight where patina has been worn away by mechanical pressure over centuries. "
    "The bronze alloy surface shows three distinct zones — deep green oxidation in the protected "
    "valleys, layered brown-to-orange at the mid-face, and raw copper colour at the tooth-tip "
    "contact points. The background is pure unlit black — no texture, no gradient, no depth reference. "
    "Camera: slow pan traversing the mesh line from left to right, following the contact arc. "
    "0-4s: pan opens on the leftmost visible teeth, meshing detail resolving from the leading "
    "engagement point inward, micro-highlight at contact burning amber. "
    "4-8s: pan completes the traverse, the rightmost teeth entering frame as the left edge falls to void. "
    "[4K] [24fps] [macro] [slow pan] [aspect ratio: 16:9] "
    "[depth of field: razor-thin, tooth apex to valley] "
    "[color grade: amber highlight against pure black void] "
    "[without modern machining] [without contemporary materials] "
    "[no background fill] [no lens flare]"
),
"fallback": (
    "Ancient bronze interlocking gear teeth, amber raking side light from left, "
    "deep shadow valleys, pure black background, macro close-up, without modern machining."
),
},

"s04": {
"primary": (
    "A mind working alone at the edge of what the ancient world believed possible. "
    "Medium shot, the silhouette of an ancient Greek craftsman bent over a stone workbench "
    "in a Hellenistic workshop, the figure's outline sharp against the warm amber pool of an "
    "oil lamp burning at the bench edge, bronze tools arrayed across the work surface catching "
    "the flame. The oil lamp — a fired terracotta vessel with a single wick — produces the "
    "scene's only light source: a warm amber cone radiating from the bench, falling off rapidly "
    "to darkness in all directions, casting workshop columns into deep shadow. The bench surface "
    "carries material evidence of the work — bronze filings pale gold against grey limestone, "
    "iron tools with worn wooden handles, a small device-in-progress whose gear faces catch the "
    "lamp's warmth at an acute angle. The craftsman's linen robes catch the lamp warmth at the "
    "near shoulder and recede to silhouette on the far side. Camera: static, locked. "
    "0-4s: craftsman's hands move over the device, the lamp flame holds steady, micro-glints "
    "from bronze filings catching light at the bench surface. "
    "4-8s: figure holds position, head angled toward the work, workshop darkness deepening "
    "behind as the eye adjusts to the lamp cone. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: medium, figure to bench surface] "
    "[color grade: warm amber fire against deep workshop shadow] "
    "[without modern tools] [without electric lighting] "
    "[no background detail beyond lamp radius] [no direct face visibility]"
),
"fallback": (
    "Ancient Greek craftsman silhouette at limestone workbench, terracotta oil lamp, "
    "bronze tools, medium shot, without modern tools or electric lighting."
),
},

"s05": {
"primary": (
    "The moment of discovery that the academic world will not believe. "
    "Medium shot, a Greek archaeologist in a heavy dark wool suit leans toward a corroded "
    "bronze mass on a wooden shelf in an Athens museum storeroom, 1902, his face angled into "
    "the dim amber gaslight falling from a wall bracket above and to the left. The gas lamp "
    "produces a soft amber wash that catches the wool's surface texture — woven dark, lapels "
    "and collar holding the warmest reflection — and illuminates the bronze fragment's "
    "green-brown encrusted surface as warm ochre overlay against deeper green. Storeroom shelves "
    "behind carry other artefacts in shadow — terracotta fragments, wrapped bundles, wooden "
    "crates — forming a dense but unlit background. The archaeologist's hands are bare, fingers "
    "extended toward but not quite touching the bronze surface. Camera: static, locked. "
    "0-4s: figure's posture resolves from the gaslit scene, the bronze fragment's encrusted "
    "surface catching the amber wash, storeroom background establishing as deep shadow depth. "
    "4-8s: the archaeologist's attention tightens — the lean deepens fractionally, the bronze "
    "fragment remaining the light's focal point, the storeroom still around him. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: figure sharp, shelf background soft] "
    "[color grade: warm amber gaslight against cool dark storeroom] "
    "[without modern fluorescent lighting] [without institutional display framing] "
    "[no electric light sources] [no contemporary clothing]"
),
"fallback": (
    "Greek archaeologist in dark wool suit examining corroded bronze fragment, "
    "dim amber gaslight, Athens museum storeroom 1902, medium shot, without electric lighting."
),
},

"s06": {
"primary": (
    "A room full of certain men confronting evidence they have decided not to understand. "
    "Wide shot, five European scholars in formal 1902 academic dress — dark frock coats, high "
    "collars, watch chains — arranged around a rectangular table in a formal academic hall, "
    "one figure pushing a document toward the table centre while others gesture or lean back "
    "in dismissal. Cool diffuse daylight enters from tall windows at frame-left, falling across "
    "the table and figures without warmth — a flat grey-white northern European light that drains "
    "the scene of comfort. The table surface is dark polished wood, the document a pale cream "
    "against the dark field; the figures cast near-shadowless forms in the diffuse light. "
    "The hall background carries dark wood panelling and a high plaster ceiling, architectural "
    "detail receding to cool shadow at the room's far end. Camera: static, wide. "
    "0-4s: full table composition holds, the central document visible as the focal object, "
    "scholars' postures of dismissal legible in their arrangement. "
    "4-8s: one figure at the near end rises fractionally, posture of departure; the document "
    "remains at the table centre, unresolved. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: wide, table to hall background] "
    "[color grade: cool grey-white diffuse daylight, dark polished wood] "
    "[without modern furniture] [without contemporary academic context] "
    "[no warm ambient fill] [no artificial lighting]"
),
"fallback": (
    "European scholars in 1902 formal dress around dark wood table, "
    "cool diffuse daylight, academic hall, wide shot, without modern furniture."
),
},

"s07": {
"primary": (
    "A man who understood the sky as a machine, and built the proof. "
    "Medium shot, an ancient Greek scholar in a white wool chiton stands at a wooden reading "
    "stand in a Hellenistic library interior, a papyrus scroll partially unrolled before him, "
    "his posture erect with the authority of a man who has worked through the problem. "
    "Warm diffuse daylight filters through the spaces between marble columns at the room's far "
    "end, entering as broad shafts of pale amber that warm the white chiton at the shoulder and "
    "chest while the lower half falls to cooler shadow. The marble columns are pale Pentelic "
    "white with faint grey veining, their surfaces worn smooth; the wooden reading stand is "
    "dark-stained cedarwood with iron fittings. The papyrus scroll carries dark ink geometric "
    "diagrams — circles, angles, numerical notation — visible in the column-diffused daylight. "
    "Camera: static. The scholar faces three-quarters away from frame. "
    "0-4s: scholar, stand, scroll, and column-light establish as a unit; daylight shaft "
    "holds warm amber consistency. "
    "4-8s: scholar's hand moves to a point on the scroll diagram, papyrus surface catching "
    "the diffuse light as warm cream. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: medium, scholar to column background] "
    "[color grade: warm amber daylight against cool marble shadow] "
    "[without modern materials] [without contemporary academic context] "
    "[no artificial light sources] [no direct-to-camera gaze]"
),
"fallback": (
    "Ancient Greek scholar in white chiton at cedarwood reading stand, Hellenistic marble library, "
    "warm column-diffused daylight, medium shot, without modern materials."
),
},

"s08": {
"primary": (
    "Objects that have survived two thousand years to ask a single question. "
    "Overhead close-up, corroded bronze mechanism fragments arranged on a dark matte surface, "
    "shot from directly above, the composition implying a radial arrangement that suggests "
    "interrogation without literalism. A single overhead spotlight descends in a tight cone, "
    "illuminating the fragments at its centre and falling off sharply to darkness at the frame "
    "edges; each fragment casts a hard-edged shadow inward toward the composition's centre. "
    "The bronze surfaces carry dense green-brown marine patina, fragment edges where they "
    "fractured showing pale copper-orange cross-sections; fine calcite deposits form pale cream "
    "crusts in the protected internal angles. The dark surface beneath the fragments is matte "
    "black — no reflection, no sheen, absorbing all light that escapes the fragments. "
    "Camera: static, locked overhead. "
    "0-4s: the overhead spotlight establishes — fragments emerge from surrounding darkness "
    "as the cone of light resolves patina textures, shadow lines hardening. "
    "4-8s: the composition holds, spotlight centre burning brightest at the most-patinated "
    "fragment face, surrounding darkness deepening. "
    "[4K] [24fps] [overhead] [aspect ratio: 16:9] "
    "[depth of field: shallow, fragment surfaces across the overhead plane] "
    "[color grade: amber spotlight centre against pure black] "
    "[without modern display labelling] [without institutional context] "
    "[no side fill] [no reflected light from surrounding surfaces]"
),
"fallback": (
    "Corroded bronze mechanism fragments overhead on matte black surface, "
    "single overhead spotlight, green-brown marine patina, without institutional display context."
),
},

"s09": {
"primary": (
    "The sea that kept the secret for two thousand years. "
    "Wide shot, a Greek island cliff edge runs from the lower-left frame corner toward the "
    "horizon at upper-right, pale sun-bleached limestone fractured and layered, salt-crusted "
    "at the wave-cut base, amber-warm in the golden-hour light above. The Aegean spreads "
    "outward to the full horizon — deep cerulean blue in the middle distance transitioning to "
    "warm copper-gold at the horizon line where the low sun strikes the water at a near-flat angle. "
    "Golden hour light enters from the right at a very low angle, raking across the cliff face "
    "and drawing every fissure and salt-crust ridge into high relief; the sea surface fractures "
    "the same warm light into a field of scattered amber points. Camera: slow pan left, moving "
    "across the cliff edge and opening the sea horizon. "
    "0-4s: pan opens on the cliff face in detail, amber side light drawing the limestone "
    "texture into sharp relief, the sea visible behind. "
    "4-8s: pan continues toward the open horizon, the cliff falling to frame-left edge "
    "as the full copper-gold sea surface expands across the frame. "
    "[4K] [24fps] [slow pan] [aspect ratio: 16:9] "
    "[depth of field: wide, cliff face to horizon] "
    "[color grade: amber-copper golden hour against cerulean-deep blue sea] "
    "[without modern coastal development] [without contemporary signage] "
    "[no artificial light sources] [no human figures]"
),
"fallback": (
    "Greek island limestone cliff edge, Aegean Sea horizon, golden hour amber-copper light, "
    "wide shot slow pan, without modern coastal development."
),
},

"s10": {
"primary": (
    "A man of the Roman world reading a text that proved something they had already seen. "
    "Medium shot, a Roman man in a white wool toga with a narrow purple stripe reads from a "
    "papyrus scroll partially unrolled in his hands, standing in a marble-floored villa atrium. "
    "Warm diffuse daylight enters through the impluvium opening above the atrium's centre, "
    "descending as a broad warm column that falls across the white toga and the scroll surface; "
    "the surrounding columns cast thin shadows at regular intervals across the marble floor. "
    "The toga is heavy wool — white with natural fibre variation visible in the draped folds, "
    "the purple stripe catching the warm overhead light as deep violet. The papyrus scroll is "
    "ochre-cream, slightly curling at the rolled edges, with dense ink text in careful Roman "
    "script visible in the lit zone. Camera: static. The figure faces slightly away from direct frame. "
    "0-4s: figure, scroll, and atrium daylight establish; the impluvium light column "
    "holds consistent warmth across the toga surface. "
    "4-8s: the figure's hands shift the scroll to reveal more text, papyrus surface "
    "catching the warm overhead light as warm cream. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: medium, figure to atrium columns] "
    "[color grade: warm atrium daylight, white wool and ochre-cream tones] "
    "[without modern materials] [without direct-to-camera gaze] "
    "[no contemporary objects] [no artificial fill]"
),
"fallback": (
    "Roman man in white toga with purple stripe reading ochre papyrus scroll, "
    "marble villa atrium, warm impluvium daylight, medium shot, without modern materials."
),
},

"s11": {
"primary": (
    "A Roman senator holding in his hands something that should not yet exist. "
    "Medium close-up, a Roman senator in a white wool toga with a wide purple stripe examines "
    "a small bronze spherical instrument held in both hands, in the colonnaded portico of a "
    "marble villa. Warm atrium daylight descends from the open sky, falling across the white "
    "toga's upper folds and illuminating the bronze instrument's surface in warm amber — the "
    "sphere's curved face showing green-brown patina with worn copper-orange highlights at the "
    "raised features. The marble columns at the portico edge are pale cream-white, worn at the "
    "base where decades of sandal traffic smoothed the stone; the column shafts hold daylight "
    "warmly at their facing surfaces, shadows deepening on their far sides. The senator's hands "
    "are bare — the instrument resting in open palms, thumbs at rest, not gripping. Camera: static. "
    "0-4s: figure, instrument, and column-framed daylight establish; the bronze sphere's surface "
    "detail resolves in the warm light, patina texture sharpening. "
    "4-8s: senator's gaze remains on the instrument, bronze surface catching warm daylight "
    "at the highest-relief features, surrounding marble holding pale warmth. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: shallow-medium, instrument surface to senator's hands] "
    "[color grade: warm amber against marble cream-white] "
    "[without modern objects] [without contemporary clothing] "
    "[no electrical light sources] [no modern backgrounds]"
),
"fallback": (
    "Roman senator in white toga with purple stripe holding small bronze spherical instrument, "
    "marble villa columns, warm atrium daylight, medium close-up, without modern objects."
),
},

"s12": {
"primary": (
    "Two of them. Side by side. The shock of the second makes the first extraordinary. "
    "Wide medium shot, two small bronze geared astronomical instruments rest on a Roman marble "
    "shelf — each approximately the size of a large fist — their patinated surfaces showing "
    "identical conditions of weathering, green-brown marine patina even across both objects "
    "suggesting they are contemporaneous. Warm amber side light enters from the left, raking "
    "across the shelf's white marble surface and illuminating both instruments in identical warm "
    "tones, their shadows falling parallel to the right — twin shadows confirming the impossible "
    "fact of duplication. The shelf is polished marble with fine grey veining; the wall behind "
    "is warm Roman plaster frescoed in dark red and ochre. The instruments sit two hands-width "
    "apart, neither dominant — the composition deliberately balanced to prevent a primary reading. "
    "Camera: static. "
    "0-4s: both instruments establish in the frame simultaneously, the side light resolving "
    "their surface patina in matching warmth, twin shadows at identical angles. "
    "4-8s: the composition holds without change — the fact of two instruments together "
    "is the entire content of the scene. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: medium, instruments to fresco wall] "
    "[color grade: amber side light against marble white and fresco ochre-red] "
    "[without modern display labels] [without institutional context] "
    "[no asymmetric framing] [no focus differential between the two objects]"
),
"fallback": (
    "Two small bronze geared astronomical instruments on Roman marble shelf, "
    "warm amber side light, twin shadows, wide medium shot, without display labels."
),
},

"s13": {
"primary": (
    "The same question asked twice, and answered differently. "
    "Wide shot, an ancient stone-paved road forks into two diverging paths across a Mediterranean "
    "landscape — the paving stones are local grey limestone, worn smooth by centuries of foot and "
    "wheel traffic, their joints filled with pale dried soil. Harsh noon sun descends at "
    "near-vertical angle, pressing shadows directly beneath every stone and scrub plant, the "
    "landscape bleaching toward white at the sky horizon and holding dusty grey-green in the low "
    "Mediterranean scrub that borders both paths. The near road surface fills the lower third of "
    "the frame — paving visible in sharp detail, dust settled into joints, a faint wheel rut along "
    "the left edge of the left branch. Both paths narrow toward their respective horizons, "
    "their destinations obscured. Camera: static. "
    "0-4s: the road fork establishes — both branches visible from the splitting point forward, "
    "noon light pressing the stone surface flat and pale, scrub shadow minimal. "
    "4-8s: the scene holds, the two paths receding in parallel toward separate horizons, "
    "the silence of an empty road at midday. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: wide, near stone to far horizon] "
    "[color grade: bleached noon white, grey-green Mediterranean scrub, pale sky] "
    "[without human figures] [without modern road markings] "
    "[no modern vehicles or signage] [no non-Mediterranean vegetation]"
),
"fallback": (
    "Ancient grey limestone road forking into two paths, Mediterranean scrubland, "
    "harsh noon sun, wide shot, without human figures or modern markings."
),
},

"s14": {
"primary": (
    "Evidence submitted to a court that is still deliberating. "
    "Extreme close-up, a corroded bronze gear fragment fills the frame — its face turned to show "
    "the calibration marks and tooth spacing that define its impossibility. Clinical white light "
    "descends from directly above with even, shadowless coverage, eliminating raking drama in "
    "favour of forensic legibility: every graduation mark, every tooth edge, every pit in the "
    "patina surface rendered in flat, accurate detail. The bronze surface under white light reveals "
    "its full oxidation stratigraphy — deep green in the protected valleys, mid-brown on "
    "intermediate faces, ochre-orange at the high points where patina is thinnest. "
    "Between the gear teeth, fine graduation lines are scored into the metal — their depth and "
    "regularity visible as darker grooves against the patina field. The fragment's fracture edge "
    "shows a pale copper cross-section, the internal alloy composition exposed. Camera: static, "
    "locked overhead. "
    "0-4s: clinical white light establishes full forensic legibility across the fragment surface, "
    "calibration marks resolving from the central graduation field outward to the tooth edges. "
    "4-8s: the light holds, the fragment completely still, graduations visible in flat white "
    "illumination as precise as the instrument that made them. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: very shallow, calibration marks on one focal plane] "
    "[color grade: clinical white, full oxidation spectrum from deep green to copper] "
    "[without atmospheric lighting] [without period setting] "
    "[no warm fill] [no shadow casting]"
),
"fallback": (
    "Corroded bronze gear fragment with calibration marks and scored graduation lines, "
    "clinical white overhead light, extreme close-up, without atmospheric or warm fill lighting."
),
},

"s15": {
"primary": (
    "Knowledge passed forward through the pressure of a craftsman's fingers. "
    "Close-up of craftsman hands at work on a small bronze geared device on a wooden workshop "
    "bench, 1st century BCE Hellenistic period. Oil lamp amber light enters from the upper left "
    "in a tight warm cone, illuminating the hands and device surface directly while the bench "
    "edges fall to warm shadow; the lamp is a terracotta single-wick vessel, its flame providing "
    "the scene's exclusive light source. The hands are bare — calloused at the fingertip pads "
    "and base of palm, with bronze filing embedded in the skin creases and traces of verdigris — "
    "green-grey oxidation — at the thumbs where the craftsman handles the device daily. "
    "The device itself is small — the span of two hands — its gear faces showing tool-fresh copper "
    "brightness on the working surfaces and green patina in the protected recesses. "
    "The bench is cedarwood, dark-stained, with metal filings scattered across its surface. "
    "Camera: static. Only the hands move. "
    "0-4s: the hands position a small gear component against the device body, the oil lamp amber "
    "resolving tool-fresh copper surface of working faces against the patinated field. "
    "4-8s: the craftsman applies micro-pressure with one thumb against the gear face, the device "
    "held steady in the other hand, lamp light catching knuckle prominences as warm amber highlights. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: shallow, hands to device surface] "
    "[color grade: warm oil lamp amber against deep bench shadow] "
    "[without modern tools] [without contemporary workshop context] "
    "[no synthetic materials] [no electrical light sources]"
),
"fallback": (
    "Ancient craftsman calloused hands repairing small bronze geared device on cedarwood bench, "
    "terracotta oil lamp from upper left, close-up, without modern tools."
),
},

"s16": {
"primary": (
    "The thing the ancient world made that the modern world took two thousand years to recreate. "
    "Macro close-up, ancient bronze gear teeth in a single perfect register across the frame — "
    "their uniform spacing and precisely cut profiles occupying the central horizontal band. "
    "Amber raking side light enters from the left at a very low angle, burning along the top edge "
    "of each tooth and driving deep shadow into the valley between each pair; tooth-tip highlights "
    "are near-white amber and valley shadows are near-black, the contrast ratio extreme. "
    "The bronze alloy surface carries distinct oxidation zones along the tooth face — deep green "
    "marine patina at the protected bases, mid-brown at the mid-face where slight abrasion has "
    "thinned the crust, copper-orange at the tool-cut apices where original metal is nearest the "
    "surface. The background is unlit black — no gradient, no reflection. Camera: static. "
    "0-4s: the tooth register holds across the frame, amber raking light resolving each tooth's "
    "precise geometry from tip to valley, shadow deepening as the light angle establishes. "
    "4-8s: the composition locks — each tooth identical in profile, spacing regular, the raking "
    "light making the case for precision that no text could. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: razor-thin, tooth apex plane] "
    "[color grade: amber tooth highlights against pure black shadow and void] "
    "[without modern machining context] [without scale reference] "
    "[no ambient fill] [no background detail]"
),
"fallback": (
    "Ancient bronze gear teeth in uniform register, amber raking side light from left, "
    "deep shadow valleys, pure black background, macro close-up, without modern machining context."
),
},

"s17": {
"primary": (
    "Not a unique object. A trade good. Something ordered, invoiced, and shipped. "
    "Wide establishing shot, a Roman merchant vessel's main deck spreads across the frame — "
    "rough-hewn grey-brown oak planking with iron fittings, amphorae lashed in rows against "
    "the hold covers, wooden cargo crates roped in place at the deck's centre. "
    "Bright noon Mediterranean sun descends at high angle, pressing sharp shadows directly below "
    "every object on the deck — amphorae cast short ellipses of shadow, the mast and rigging "
    "throw geometric cross-patterns of rope-shadow across the deck planking. "
    "The deck planking is weathered grey-brown oak, bleached pale at the surface from salt and "
    "sun, the iron deck fittings rust-orange. The amphorae are terracotta-red, some corked with "
    "pale clay, their surfaces carrying the salt bloom of open-sea travel. "
    "The sea beyond the hull is flat and brilliant — Mediterranean noon blue with near-white glare. "
    "Camera: slow pan across the deck. "
    "0-4s: pan opens at the bow, deck planking and cargo arrangement establishing — amphorae rows, "
    "rope lashing, noon shadow geometry across the oak surface. "
    "4-8s: pan reaches the cargo crates at the deck centre, the packed and invoiced reality of "
    "ancient trade framed in terms of cargo-deck practicality. "
    "[4K] [24fps] [slow pan] [aspect ratio: 16:9] "
    "[depth of field: wide, near deck to sea horizon] "
    "[color grade: bleached noon, grey oak and terracotta-red against Mediterranean blue] "
    "[without modern shipping materials] [without contemporary cargo] "
    "[no modern harbour in background] [no anachronistic deck equipment]"
),
"fallback": (
    "Roman merchant vessel deck with terracotta amphorae and oak cargo crates, "
    "Mediterranean noon sun, slow pan, wide shot, without modern shipping materials."
),
},

"s18": {
"primary": (
    "Not one man. Many. A production floor, not a myth. "
    "Wide interior shot, a large Hellenistic workshop with stone-and-timber construction: "
    "three stone workbenches in a row, each with a craftsman in rough undyed linen working "
    "over a bronze assembly, oil lamps burning at each bench. Multiple oil lamp sources create "
    "overlapping amber pools — each bench lit independently, the intermediate floor falling to "
    "warm dimness, the workshop walls and ceiling receding into deep orange-brown shadow above "
    "the lamp radius. The craftsmen are in plain short-cut chiton — each posture angled toward "
    "their individual bench work: one grinding, one fitting, one checking with a measuring tool. "
    "The bench surfaces carry material evidence of coordinated production: bronze filings "
    "organised by stage of work, tools laid at consistent positions suggesting procedure, "
    "not improvisation. Camera: static, wide. "
    "0-4s: the full workshop interior establishes — three benches in perspective, three lamp "
    "cones, three figures, the scale of the production space legible from the architecture. "
    "4-8s: the middle craftsman's lamp holds as the closest and brightest source, the workshop "
    "depth visible behind him to the darkened far wall. "
    "[4K] [24fps] [aspect ratio: 16:9] [depth of field: wide, near bench to far wall] "
    "[color grade: multiple overlapping amber oil lamp pools against warm-dark workshop interior] "
    "[without modern tools] [without contemporary workshop materials] "
    "[no electric lighting] [no anachronistic safety equipment]"
),
"fallback": (
    "Three ancient Greek craftsmen at stone workbenches in Hellenistic workshop, "
    "multiple oil lamp pools, wide interior shot, without modern tools or electric lighting."
),
},

"s19": {
"primary": (
    "The city that made the thing. "
    "Wide establishing shot, the Hellenistic harbour of ancient Rhodes stretches across the frame "
    "— a pale grey limestone quay running left to right, merchant vessels moored at stone bollards, "
    "their hulls dark-weathered pitch-treated timber with ochre-yellow painted waterlines. "
    "Golden hour amber light enters from the right at a very low angle, raking across the quay's "
    "limestone surface and drawing every paver joint and iron mooring ring into sharp relief; "
    "the harbour water is warm copper-gold at the surface, deep blue-black in the shadow of hulls. "
    "The quay stone is pale grey limestone, worn smooth along the traffic path but rough at the "
    "edges; iron mooring rings are rust-stained, the rust bleeding across stone in long ochre "
    "streaks. The vessel hulls are dark pitch-treated timber, upper strakes ochre-yellow or "
    "red-brown, colours absorbing decades of Mediterranean sun and salt. Camera: slow pan left "
    "across the harbour, following the quay line. "
    "0-4s: pan opens on the far quay end, full harbour establishing in golden hour light, "
    "vessels and quay stone in warm amber. "
    "4-8s: pan reaches the near vessels, hull timbers filling frame-right as harbour water "
    "copper-gold extends to the left. "
    "[4K] [24fps] [slow pan] [aspect ratio: 16:9] "
    "[depth of field: wide, quay stone to harbour horizon] "
    "[color grade: golden amber hour against dark harbour water and pale limestone] "
    "[without modern harbour infrastructure] [without contemporary vessels] "
    "[no modern signage] [no anachronistic materials]"
),
"fallback": (
    "Ancient Hellenistic Rhodes harbour with pitch-timber merchant vessels at pale limestone quay, "
    "golden hour amber light, wide establishing slow pan, without modern harbour infrastructure."
),
},

"s20": {
"primary": (
    "The mechanism did not travel by accident. Someone planned its route. "
    "Overhead close-up, an ancient papyrus map of Mediterranean trade routes fills the frame "
    "— the sheet is aged ochre-cream, its surface slightly rough with papyrus fibre texture "
    "visible at the margins, ink trade-route lines and port notations dark brown against the "
    "cream field. Warm overhead amber light descends in an even cone, illuminating the map "
    "surface consistently without raking shadow — a practical reading light. "
    "The papyrus shows its age as a material object: faint cracking along horizontal fibre "
    "lines, a slight curl at the right edge from repeated rolling and unrolling, a water-stain "
    "shadow along the lower margin. The ink markings are dense: thick route lines connecting "
    "coastal points, fine Greek script annotations at the port nodes, numerical notations "
    "beside the route indicators. Camera: static, overhead. "
    "0-4s: the map surface establishes in warm overhead light, the full route network visible "
    "from the overhead perspective, ink markings resolving from the central Mediterranean "
    "line outward to the coastal annotations. "
    "4-8s: the overhead light holds even, papyrus texture and ink line quality visible at rest. "
    "[4K] [24fps] [overhead] [aspect ratio: 16:9] "
    "[depth of field: even across the flat papyrus surface] "
    "[color grade: warm amber overhead against ochre-cream papyrus and dark brown ink] "
    "[without modern maps or projections] [without contemporary notation] "
    "[no cast shadows from overhead vantage] [no modern materials beneath the papyrus]"
),
"fallback": (
    "Ancient ochre-cream papyrus map with dark ink Mediterranean trade routes, "
    "warm overhead amber light, overhead close-up, without modern maps or contemporary notation."
),
},

"s21": {
"primary": (
    "The theory that made the instrument possible, worked out in a room that no longer exists. "
    "Medium shot, an ancient Greek scholar in a white linen chiton stands at a reading stand in "
    "a Hellenistic library, a large papyrus sheet spread flat before him carrying geometric gear "
    "diagrams in ink — circles, epicycle arcs, angular divisions, numerical annotations. "
    "Warm diffuse daylight enters through the column-framed openings at the room's far end, "
    "arriving as broad gentle amber shafts that warm the left side of the chiton and the upper "
    "papyrus surface. The library columns are pale marble; the reading stand is dark cedarwood. "
    "The papyrus sheet is large — the scholar's full arm-width — and carries diagrams of "
    "interlocking circular systems that are unmistakably gear-train geometry. "
    "The scholar's hands rest flat on the table at either side of the sheet, not touching it. "
    "Camera: static. "
    "0-4s: scholar and diagram establish — the papyrus gear geometry resolving in warm daylight, "
    "column-light shafts visible behind the figure as warm amber verticals. "
    "4-8s: scholar's gaze holds on the diagram, library still around him; the gear diagrams "
    "remain the visual centre of the composition. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: medium-shallow, scholar and diagram to column background] "
    "[color grade: warm diffuse daylight amber against pale marble and cream papyrus] "
    "[without modern materials] [without contemporary notation] "
    "[no artificial fill] [no direct-to-camera gaze]"
),
"fallback": (
    "Ancient Greek scholar in white linen chiton studying gear-train geometry on large papyrus, "
    "Hellenistic marble library, warm column-diffused daylight, medium shot, without modern materials."
),
},

"s22": {
"primary": (
    "Two thousand years reduced to the weight of an object in a single hand. "
    "Close-up, a single human hand holds a corroded bronze gear fragment in an open palm "
    "against a dark matte surface. A single warm spotlight descends from directly above, "
    "illuminating the hand and the object while everything beyond the immediate radius falls "
    "to darkness. The hand is bare — adult, calloused at the fingertip pads and base of palm, "
    "with the fine creasing of skin visible at the knuckles; bronze filing is embedded in "
    "the skin creases and traces of verdigris at the thumbs where the fragment is handled daily. "
    "The bronze fragment surface — dense green-brown marine patina, pale cream calcite deposits "
    "at interior angles, copper-orange at fractured edges — is elevated to particular significance "
    "by the warmth and immediacy of the skin it rests against. The dark surface beneath absorbs "
    "all reflected light. Camera: static, locked close. "
    "0-4s: the hand establishes in the spotlight, the fragment's patina surface resolving in "
    "warm amber, the skin-to-metal proximity intimate and immediate. "
    "4-8s: the hand holds without motion — fingers curled slightly but not gripping, the fragment "
    "resting in the open palm, the spotlight warmth consistent. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: shallow, hand surface to fragment face] "
    "[color grade: warm amber spotlight against deep shadow, skin tones and bronze patina] "
    "[without modern context] [without institutional display] "
    "[no background detail] [no motion in the hand]"
),
"fallback": (
    "Single calloused human hand holding corroded bronze gear fragment in open palm, "
    "single warm overhead spotlight, dark background, close-up, without institutional display context."
),
},

"s23": {
"primary": (
    "The question that outlasts everyone who might have answered it. "
    "Wide shot, an empty ancient stone workbench in an abandoned Hellenistic workshop — the "
    "bench surface bare except for scattered bronze filings and two iron tools left at an angle "
    "suggesting mid-work interruption rather than end-of-day storage. Fading amber light enters "
    "from a high window opening at the left edge of the frame, arriving at a very low angle "
    "characteristic of late afternoon, warm but diminishing — more presence of warmth than intensity. "
    "The bench is grey limestone, its surface worn smooth from decades of work, tool-contact zones "
    "polished to a faint sheen while the edges remain rough. The abandoned tools are iron with "
    "worn wooden handles — a graver and a dividing tool — positioned mid-work. Fine dust has "
    "settled across the bench surface and floor below, confirming extended absence without "
    "dramatic disturbance. Camera: static. "
    "0-4s: the empty bench establishes in fading amber window light — tools visible at their "
    "last position, bronze filings catching low warm light as scattered gold points, "
    "dust surface confirming silence. "
    "4-8s: the light holds at its low angle, the bench remaining still, the two tools in their "
    "mid-work position, the question of what stopped the work unresolved. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: wide, near bench to workshop wall] "
    "[color grade: fading amber window light against cool grey stone and deep workshop shadow] "
    "[without modern context] [without contemporary objects] "
    "[no human presence] [no artificial fill]"
),
"fallback": (
    "Empty grey limestone workshop bench with iron graver and dividing tool mid-position, "
    "fading amber window light, fine dust settled, wide shot, without human presence."
),
},

"s24": {
"primary": (
    "Not a rarity. A product line. "
    "Medium shot, multiple small bronze geared astronomical devices arranged on a cedarwood "
    "workshop shelf in a Rhodian workshop interior, 1st-2nd century BCE. An oil lamp burns "
    "at the near-left end of the shelf, casting its amber cone across the device row — the "
    "nearest device fully lit in warm amber, the devices further down the shelf receiving "
    "progressively less light, the last near the right edge of the frame held only in warm dimness. "
    "The shelf is dark from lamp smoke, with the impressions of previous objects visible in dust "
    "as rings and rectangles. Each device is similar in size — span of two hands — with "
    "green-brown patina accumulation on protected surfaces and tool-fresh copper brightness "
    "on working faces suggesting recent manufacture or service. Camera: static. "
    "0-4s: the full shelf establishes, the oil lamp amber falling from left to right, "
    "the row of devices resolving from the nearest to the furthest, workshop depth visible "
    "beyond the shelf. "
    "4-8s: the lamp flame holds, devices motionless on the shelf, their number and uniformity "
    "— the fact of multiple copies — as their sole narrative function. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: shallow-medium, near device sharp, far devices in progressive soft focus] "
    "[color grade: warm oil lamp amber falling off across device row, deep workshop shadow beyond] "
    "[without modern context] [without contemporary materials] "
    "[no electric lighting] [no labels or signage]"
),
"fallback": (
    "Multiple small bronze geared astronomical devices on smoke-dark cedarwood workshop shelf, "
    "oil lamp amber falling across the row, Rhodian workshop, medium shot, without labels or modern context."
),
},

"s25": {
"primary": (
    "The objects that waited. "
    "Close-up, corroded bronze fragments rest on a pale limestone surface in a diffuse "
    "blue-green light environment suggesting the underwater world without literalism — the "
    "light arrives as scattered, directionless cold blue-green with no single source, "
    "the way deep coastal water scatters Mediterranean sunlight. The rock surface beneath "
    "the fragments is water-smoothed pale limestone covered with pale grey coralline algae "
    "deposits and fine sediment settled into every horizontal crevice. The bronze fragments "
    "carry two distinct oxidation histories: thick marine concretions — calcium-grey, rough — "
    "cover the majority of each fragment's exterior surface, with the original bronze and its "
    "green-brown patina visible only at points where the concretion has broken away, revealing "
    "the colour of the thing beneath. Camera: static. "
    "0-4s: the fragments establish in diffuse blue-green light, the marine concretion surface "
    "resolving as a pale grey-calcium crust, underlying bronze patina visible at the worn points. "
    "4-8s: the light holds in its scattered cold quality, the fragments absolutely still — "
    "the same stillness they maintained for two thousand years. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: shallow, fragment surfaces across the rock plane] "
    "[color grade: cold scattered blue-green, calcium-grey marine concretion, bronze patina reveal] "
    "[without modern surface context] [without artificial fill light] "
    "[no warm colour sources] [no motion in water or sediment]"
),
"fallback": (
    "Corroded bronze fragments with calcium-grey marine concretion on water-smoothed limestone, "
    "cold scattered blue-green diffuse light, close-up, without warm fill or artificial light sources."
),
},

"s26": {
"primary": (
    "The instruments that made the instrument. "
    "Close-up, ancient iron precision tools arranged on a grey limestone workshop bench surface "
    "— a dividing tool, a graver with a pointed bronze tip, a wooden layout compass with iron "
    "points — their positioning suggesting active use rather than display. Oil lamp raking light "
    "enters from the lower-left at a very low oblique angle, catching the iron tool shafts along "
    "their length as sharp amber lines and throwing the bench surface into deep shadow behind "
    "each tool. The iron tools show their working life: wooden handles dark from hand oil and "
    "warmth, worn smooth at the grip points; iron shafts carrying surface rust at the unexposed "
    "areas and polished brightness at the contact points where the craftsman's grip keeps the "
    "metal clean. The stone bench surface carries accumulated metalworking traces — fine brass "
    "and iron filings embedded in the stone pores, scratch marks from previous work sessions. "
    "Camera: static. "
    "0-4s: the tool arrangement establishes in raking lamp light, each shaft catching amber "
    "along its length, the bench surface coming forward in the low raking angle. "
    "4-8s: the lamp light holds, tools motionless in their working positions — the dividing "
    "tool's precision tip closest to the viewer, catching lamp warmth as a bright amber point. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: shallow-medium, tool tips to handle ends] "
    "[color grade: warm oil lamp amber raking across iron and grey stone] "
    "[without modern tools] [without contemporary workshop context] "
    "[no synthetic materials] [no electric light sources]"
),
"fallback": (
    "Ancient iron dividing tool, graver, and wood-iron compass on grey limestone bench, "
    "oil lamp raking light from lower-left, close-up, without modern tools or electric lighting."
),
},

"s27": {
"primary": (
    "A workshop that stopped without announcement. "
    "Wide shot, three empty stone workbenches stand in a row across a Hellenistic workshop "
    "interior — their surfaces bare, tools removed, bronze filings and fine metal dust still "
    "on the ground below where feet no longer stand. A doorway at the far wall stands open "
    "to bright Aegean daylight, the opening a sharp-edged rectangle of warm white light against "
    "the workshop's interior dimness — no lamp burning, no fire in the hearth, interior light "
    "coming only from this single opening and a high window slit at the left wall. "
    "The workshop walls are rough limestone plaster, pale grey, with the shadows of removed "
    "tool hooks visible as slightly lighter patches where hooks blocked the wall from "
    "smoke-darkening over decades. The three bench surfaces are grey stone — worn at the "
    "working centre, rough at the edges, with bronze filings embedded in the stone pores "
    "and a graver's scratch pattern visible on the central bench face. Camera: static. "
    "0-4s: the three empty benches establish in interior dimness, the doorway light holding "
    "as the scene's single bright reference, faint evidence of work history visible in bench "
    "surfaces and ground debris. "
    "4-8s: the scene holds without motion — open doorway, empty benches, ground filings, "
    "the absence complete and undramatic; the workshop simply no longer in use. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: wide, near bench to open doorway] "
    "[color grade: warm Aegean daylight at doorway against cool workshop interior dimness, grey stone throughout] "
    "[without modern context] [without destruction or drama] "
    "[no human figures] [no contemporary objects]"
),
"fallback": (
    "Three empty grey stone workbenches in Hellenistic workshop, tools removed, "
    "doorway open to bright Aegean daylight, wide shot, without human figures or destruction."
),
},

"s28": {
"primary": (
    "The second look. The one that changes everything. "
    "Medium close-up, a Greek archaeologist in a heavy dark wool suit leans close to a "
    "corroded bronze fragment on a wooden shelf in an Athens museum storeroom, 1902, angling "
    "his gaze to bring the gear tooth edge into the dim amber gaslight falling from a wall "
    "bracket above. The gaslight produces a soft amber glow — not a reading light, barely a "
    "working light — that illuminates the near side of the archaeologist's face in warm amber "
    "and falls across the bronze fragment's surface, the gear teeth picking up the low amber "
    "at their leading edges while the valleys fall to darkness. The fragment is held in the "
    "archaeologist's right hand — bare fingers positioning the object to catch the gaslight "
    "at the gear tooth edge — green-brown patina visible in amber warmth and deep shadow. "
    "The storeroom shelf behind carries other artefacts in near-darkness — shapes legible as "
    "boxes and bundles but without detail. Camera: static, locked at medium close. "
    "0-4s: the archaeologist's profile and the bronze fragment establish in gaslight, the gear "
    "tooth edge resolving in low amber warmth, storeroom darkness framing the composition. "
    "4-8s: the archaeologist's head angle tightens — the lean toward the gear tooth edge "
    "fractionally deepening — the moment of recognition approaching without announcement. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: shallow, archaeologist's near eye to bronze fragment surface] "
    "[color grade: soft dim amber gaslight against deep storeroom shadow, dark wool and green-brown bronze] "
    "[without modern lighting] [without electric fixtures] "
    "[no clinical brightness] [no contemporary objects]"
),
"fallback": (
    "Greek archaeologist in dark wool suit examining corroded bronze gear teeth, "
    "dim amber gaslight, Athens museum storeroom 1902, medium close-up, without electric lighting."
),
},

"s29": {
"primary": (
    "Two thousand years of absence, rendered as architecture. "
    "Wide shot, a long stone corridor extends from the foreground deep into the frame's "
    "distance — the walls are rough-cut stone blocks, the floor the same material worn smooth "
    "at the traffic centre, the ceiling low and arching. Near-total darkness fills the corridor "
    "from the foreground to approximately two-thirds depth; at the far end, a single point of "
    "amber light — indeterminate in source, small, holding warmth without brightness — resolves "
    "at the vanishing point. The stone walls in the near foreground are barely legible — their "
    "block joints and surface texture visible only where the distant light provides the faintest "
    "reflected ambient; the middle section is pure black, no surface detail recoverable. "
    "The corridor has no branching, no doorways visible, no furnishing — it is corridor only, "
    "depth only, the single light as the sole destination. Camera: static. "
    "0-4s: composition establishes — foreground stone, corridor darkness deepening to full black "
    "in the middle section, single distant amber point at the vanishing end. "
    "4-8s: the single distant light holds without change — the darkness between the viewer and "
    "the light resolving as total; the corridor holds its silence. "
    "[4K] [24fps] [aspect ratio: 16:9] "
    "[depth of field: wide, foreground stone to distant light point] "
    "[color grade: near-total black with single amber point at depth] "
    "[without human figures] [without decorative detail] "
    "[no intermediate light sources] [no door or window openings]"
),
"fallback": (
    "Long rough-cut stone corridor in near-total darkness, single amber light point at "
    "vanishing depth, wide shot, without human figures or intermediate light sources."
),
},

"s30": {
"primary": (
    "A relic of impossible precision suspended at the edge of recorded history. "
    "Extreme macro close-up, a single corroded bronze gear fragment with dense green-brown "
    "marine patina and copper-orange worn edges occupies the left third of a 16:9 frame against "
    "an absolute dark void. Warm amber directional light enters from the upper left at a low "
    "oblique angle, raking across the corroded ridges and casting micro-shadow relief into the "
    "gear tooth valleys; the opposing face recedes into deep surrounding shadow with no ambient "
    "fill, a hard falloff to pure unlit black. The bronze surface carries layered oxidation — "
    "green-brown marine patina at depth, copper-orange exposed at worn tooth edges, fine radial "
    "cracks from the central bore, and microscopic pit corrosion that absorbs light rather than "
    "reflecting it. The void behind the fragment is absolute — no surface, no gradient, no depth "
    "haze; subject occupies one-third of frame with breathing room below and to the right. "
    "Camera: static, locked, zero drift. Subject: motionless. The only motion is a slow thermal "
    "shift in light temperature as it diffuses across the patina. "
    "0-4s: fragment surfaces from absolute darkness as light resolves detail across corroded face, "
    "patina micro-texture sharpening progressively. "
    "4-8s: light temperature deepens from amber to copper-orange, shadow edges harden along gear "
    "teeth, void consolidates to pure negative space. "
    "[4K] [24fps] [macro] [depth of field: shallow, gear teeth to patina face] "
    "[color grade: warm amber-copper against pure black] [aspect ratio: 16:9] "
    "[without modern surfaces] [without institutional display context] "
    "[no ambient fill] [no background detail] [no lens flare] [no motion blur]"
),
"fallback": (
    "Corroded bronze gear fragment, green-brown marine patina, amber directional light from "
    "upper left, deep shadow void, macro close-up, without modern surfaces."
),
},

"s31": {
"primary": (
    "An object that held its question for two thousand years, pointed at the sea that kept it. "
    "Wide shot, a corroded bronze gear fragment rests on a pale limestone ledge at a Greek island "
    "cliff edge — the fragment in the lower-left foreground while the vast Aegean Sea fills the "
    "right two-thirds of the frame and the full background, the horizon line at mid-frame. "
    "Golden hour amber light enters from the right at a near-flat angle, raking across the "
    "limestone ledge surface and the fragment simultaneously — the stone catching warmth as "
    "pale amber-cream, the bronze fragment's patinated face burning in deep copper-amber at its "
    "lit surfaces and falling to rich shadow in its recesses. The fragment's gear teeth profile "
    "is visible against the sea — the specific tooth-cut geometry that defines the mechanism's "
    "identity, in miniature against the immensity of the Aegean. The sea beyond is warm "
    "copper-gold at the illuminated surface, deepening to cerulean in the shadow areas, "
    "the horizon line sharp. Camera: slow pan right, moving from the fragment foreground "
    "toward the open horizon. "
    "0-4s: pan opens on the fragment close — its patina surface in full golden amber, "
    "the sea visible behind establishing depth. "
    "4-8s: pan continues rightward, the fragment receding toward the left frame edge as the "
    "full Aegean horizon opens across the frame, the question and the sea together. "
    "[4K] [24fps] [slow pan] [aspect ratio: 16:9] "
    "[depth of field: shallow foreground to infinity background] "
    "[color grade: warm golden amber against cerulean-copper Aegean] "
    "[without modern coastal development] [without contemporary objects] "
    "[no human figures] [no artificial light sources]"
),
"fallback": (
    "Corroded bronze gear fragment on pale limestone ledge, vast Aegean Sea horizon behind, "
    "golden hour amber-copper light, wide shot slow pan, without modern coastal development."
),
},

}  # end AFTER


# ── Load visual package ────────────────────────────────────────────────────────
def load_main_scenes(path: Path) -> list:
    with open(path) as f:
        package = json.load(f)
    return [s for s in package["scenes"] if s.get("source_type") == "main_episode"]


# ── Build comparison text ──────────────────────────────────────────────────────
def build_comparison_txt(scenes: list) -> str:
    lines = []
    for scene in scenes:
        sid = scene["scene_id"]
        routing = scene["production_spec"]["provider_routing"]
        after = AFTER[sid]

        lines.append("=" * 90)
        lines.append(f"SCENE: {sid}")
        lines.append("=" * 90)
        lines.append("")
        lines.append("BEFORE primary_prompt")
        lines.append(routing["primary_prompt"])
        lines.append("")
        lines.append("BEFORE fallback_prompt")
        lines.append(routing["fallback_prompt"])
        lines.append("")
        lines.append("AFTER primary_prompt")
        lines.append(after["primary"])
        lines.append("")
        lines.append("AFTER fallback_prompt")
        lines.append(after["fallback"])
        lines.append("")

    lines.append("=" * 90)
    lines.append("PRODUCTION SAFETY")
    lines.append("  visual_package_ep_000.json: NOT MODIFIED")
    lines.append("  Provider calls made:        NONE")
    lines.append("  Pipeline imported:          NO")
    lines.append("  MUAPI imported:             NO")
    lines.append("=" * 90)
    return "\n".join(lines)


# ── Build master JSON ──────────────────────────────────────────────────────────
def build_master_json(scenes: list) -> dict:
    records = {}
    for scene in scenes:
        sid = scene["scene_id"]
        routing = scene["production_spec"]["provider_routing"]
        after = AFTER[sid]
        records[sid] = {
            "before": {
                "primary_prompt": routing["primary_prompt"],
                "fallback_prompt": routing["fallback_prompt"],
            },
            "after": {
                "primary_prompt": after["primary"],
                "fallback_prompt": after["fallback"],
                "prompt_blueprint": BLUEPRINT,
            },
        }
    return {
        "experiment_id": "vpd_cinematic_prompt_compare_v1",
        "source_package": str(VISUAL_PACKAGE),
        "scene_count": len(scenes),
        "scenes": records,
        "production_safety": {
            "source_package_modified": False,
            "provider_calls": 0,
            "pipeline_imported": False,
            "muapi_imported": False,
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scenes = load_main_scenes(VISUAL_PACKAGE)

    master_json_path = OUTPUT_DIR / "all_scenes_before_after.json"
    comparison_txt_path = OUTPUT_DIR / "all_scenes_comparison.txt"

    master = build_master_json(scenes)
    comparison = build_comparison_txt(scenes)

    with open(master_json_path, "w") as f:
        json.dump(master, f, indent=2)

    with open(comparison_txt_path, "w") as f:
        f.write(comparison)

    print(f"Created: {master_json_path}")
    print(f"Created: {comparison_txt_path}")
    print()
    print(comparison)


if __name__ == "__main__":
    main()
