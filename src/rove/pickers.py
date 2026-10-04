"""Read a combobox's own popup, including searchable ARIA grids."""

import json
import re

from patchright.sync_api import Error as PlaywrightError

from . import questions

# USPS Publication 28, Appendix B: https://pe.usps.com/text/pub28/28apb.htm
US_REGIONS = dict(
    line.rsplit(" ", 1)
    for line in """Alabama AL
Alaska AK
American Samoa AS
Arizona AZ
Arkansas AR
California CA
Colorado CO
Connecticut CT
Delaware DE
District of Columbia DC
Federated States of Micronesia FM
Florida FL
Georgia GA
Guam GU
Hawaii HI
Idaho ID
Illinois IL
Indiana IN
Iowa IA
Kansas KS
Kentucky KY
Louisiana LA
Maine ME
Marshall Islands MH
Maryland MD
Massachusetts MA
Michigan MI
Minnesota MN
Mississippi MS
Missouri MO
Montana MT
Nebraska NE
Nevada NV
New Hampshire NH
New Jersey NJ
New Mexico NM
New York NY
North Carolina NC
North Dakota ND
Northern Mariana Islands MP
Ohio OH
Oklahoma OK
Oregon OR
Palau PW
Pennsylvania PA
Puerto Rico PR
Rhode Island RI
South Carolina SC
South Dakota SD
Tennessee TN
Texas TX
Utah UT
Vermont VT
Virgin Islands VI
Virginia VA
Washington WA
West Virginia WV
Wisconsin WI
Wyoming WY""".splitlines()
)


def region_names(value: str, profile: dict) -> set[str]:
    names = {questions.normalized(value)}
    country = (profile.get("identity") or {}).get("country", "")
    if questions.option_matches(str(country), "United States"):
        for full, short in US_REGIONS.items():
            pair = {questions.normalized(full), short.lower()}
            if names & pair:
                return pair
    return names


def search(control, value: str):
    """Some typeaheads listen for keyup, so fill alone never refreshes their options."""
    control.fill("")
    control.press_sequentially(str(value))


def search_value(value: str, field: dict, profile: dict) -> str:
    if questions.classify(field.get("label", "")).canonical_id == "state_region":
        short = min(region_names(value, profile), key=len)
        return short.upper() if len(short) == 2 else value
    return value


def city_options(texts: list[str], city: str, profile: dict) -> list[int]:
    """Keep all matching locations: two counties or states must never become the first."""
    regions = region_names(str((profile.get("identity") or {}).get("state_region") or ""), profile)
    starts = [
        i
        for i, t in enumerate(texts)
        if questions.normalized(t.split(",")[0]) == questions.normalized(city)
        and region_matches(t.split(","), profile)
    ]
    located = [
        i
        for i in starts
        if any(questions.normalized(part) in regions for part in texts[i].split(",")[1:])
    ]
    candidates = located or starts
    county = (profile.get("identity") or {}).get("county")
    if county:
        candidates = [
            i for i in candidates if matches_county(texts[i].split(",")[1:], county, profile)
        ]
    return candidates


def region_matches(parts: list[str], profile: dict) -> bool:
    """Reject a different explicitly displayed US state, without guessing absent columns."""
    identity = profile.get("identity") or {}
    country, state = identity.get("country"), identity.get("state_region")
    if not state or not questions.option_matches(str(country or ""), "United States"):
        return True
    tail = (
        parts[-2]
        if len(parts) > 1 and questions.option_matches(parts[-1].strip(), country)
        else parts[-1]
    )
    region = questions.normalized(tail)
    known = {questions.normalized(x) for pair in US_REGIONS.items() for x in pair}
    return region not in known or region in region_names(str(state), profile)


def matches_county(parts: list[str], county: str, profile: dict) -> bool:
    """Use a county column before the observed state; rows without one add no evidence."""
    regions = region_names(str((profile.get("identity") or {}).get("state_region") or ""), profile)
    boundary = next((i for i, part in enumerate(parts) if questions.normalized(part) in regions), 0)
    if not boundary:
        return True

    def normalized(value):
        return re.sub(r"\s+(?:county|parish|borough)$", "", questions.normalized(value))

    return normalized(county) in {normalized(part) for part in parts[:boundary]}


def with_county(profile: dict, value) -> dict:
    if not isinstance(value, str) or not value.strip() or value.strip().lower() == "skip":
        return profile
    return {**profile, "identity": {**profile.get("identity", {}), "county": value.strip()}}


def exact_row(options, expected: str):
    """A changing list may reorder rows; the observed label must still name one row."""
    row = options.filter(has_text=re.compile(r"^\s*" + re.escape(expected.strip()) + r"\s*$"))
    return row if row.count() == 1 and row.is_visible() else None


CUSTOM_ROWS_JS = """ref => {
 const e=document.querySelector('[data-rove-field="'+ref+'"]');
 if(!e) return false;
 const ids=[e.getAttribute('aria-controls'),e.getAttribute('aria-owns')]
   .filter(Boolean).join(' ').split(/\\s+/);
 const linked=ids.map(id=>document.getElementById(id)).filter(Boolean);
 const roots=linked.length?linked:[e.closest('.select__container')||e.parentElement];
 const selector='li,[role=option],[class*="suggestion" i],[class*="option" i]';
 const visible=n=>n!==e && n.getClientRects().length && getComputedStyle(n).visibility!=='hidden';
 const nodes=[...new Set(roots.flatMap(r=>[...r.querySelectorAll(selector)]))].filter(visible);
 const leaves=nodes.filter(n=>!nodes.some(other=>other!==n && n.contains(other)));
 document.querySelectorAll('[data-rove-suggestion]')
   .forEach(n=>n.removeAttribute('data-rove-suggestion'));
 const rows=leaves.map((n,i)=>{
   n.setAttribute('data-rove-suggestion',String(i));return n.innerText.trim();});
 return rows.length?rows:false;
}"""


def custom_city(form, control, city: str, profile: dict) -> bool:
    """Select one observed city in this input's own popup; never take an arbitrary row."""
    try:
        handle = form.wait_for_function(
            CUSTOM_ROWS_JS, arg=control.get_attribute("data-rove-field"), timeout=6000
        )
    except PlaywrightError:
        # A genuine plain city textbox needs no suggestion selection. Explicit picker
        # semantics prevent mistaking uncommitted search text for an address choice.
        return (
            control.get_attribute("role") != "combobox"
            and not any(
                control.get_attribute(a)
                for a in ("aria-controls", "aria-owns", "aria-autocomplete")
            )
            and questions.normalized(control.input_value()) == questions.normalized(city)
        )
    try:
        texts = handle.json_value()
    finally:
        handle.dispose()
    indexes = city_options(texts, city, profile)
    if len(indexes) != 1:
        return False
    expected = texts[indexes[0]]
    row = form.locator(f'[data-rove-suggestion="{indexes[0]}"]')
    if row.count() != 1 or row.inner_text().strip() != expected:
        return False
    row.click()
    try:
        form.wait_for_function(
            """({ref,expected})=>{
             const e=document.querySelector('[data-rove-field="'+ref+'"]');
             const chosen=e?.closest('.select__container')?.querySelector('.select__single-value');
             return e?.value===expected || chosen?.innerText.trim()===expected;
            }""",
            arg={"ref": control.get_attribute("data-rove-field"), "expected": expected},
            timeout=1500,
        )
        return True
    except PlaywrightError:
        return False


COMMITTED_JS = """({ref,values}) => {
 const e=document.querySelector('[data-rove-field="'+ref+'"]');
 if(!e || e.getAttribute('aria-expanded')!=='false') return false;
 const norm=s=>(s||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
 return values.some(v=>norm(v)===norm(e.value));
}"""


def matches(label: str, value: str, field: dict, profile: dict) -> bool:
    if questions.option_matches(label.strip(), value):
        return True
    kind = questions.classify(field.get("label", "")).canonical_id
    if kind == "postal_code":
        parts = label.split(",")
        if questions.normalized(parts[0]) != questions.normalized(value) or not region_matches(
            parts, profile
        ):
            return False
        identity = profile.get("identity") or {}
        if (
            len(parts) > 1
            and identity.get("city")
            and (questions.normalized(parts[1]) != questions.normalized(identity["city"]))
        ):
            return False
        return not identity.get("county") or matches_county(parts[2:], identity["county"], profile)
    if kind == "state_region":
        return questions.normalized(label) in region_names(value, profile)
    if kind != "phone_country_code":
        return False
    # +1 is shared by several countries: the country name disambiguates the row.
    code = re.sub(r"\D", "", value)
    country = questions.normalized((profile.get("identity") or {}).get("country"))
    return bool(
        country
        and code
        and re.sub(r"\D", "", label) == code
        and questions.option_matches(re.sub(r"[+\d()]", " ", label).strip(), country)
    )


def options(form, control):
    """A linked popup owns its rows; unrelated tables can never supply a choice."""
    ids = " ".join(control.get_attribute(a) or "" for a in ("aria-controls", "aria-owns")).split()
    if ids:
        roots = form.locator(",".join("[id=" + json.dumps(i) + "]" for i in dict.fromkeys(ids)))
        return roots.locator("[role=option],[role=row],tr")
    return form.get_by_role("option")
