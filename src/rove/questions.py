"""One code-owned answer path for application-form questions.

Every question gets a canonical identity, a class, and an answer from the first source
that is allowed to answer it:

1. the approved profile
2. an answer the owner gave before to the same canonical question
3. a policy default, for plain questions only
4. a Qwen draft, for plain questions only
5. the owner, asked once

Legal and personal questions (the sensitive class) stop after step 2: no default and no
model draft ever answers them. The rules here are exact on purpose. A legal rule matches
only when every word of the label is one the rule knows, so a negation, another country
or an added condition falls through to the owner instead of being answered for him.

This module reads no state: callers pass the frozen profile and a recall function.
"""

import functools
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from . import education, form_reading

PLAIN, SENSITIVE = "plain", "sensitive"
POSITIVE, NEGATED = "positive", "negated"

REMEMBERED = "your earlier answer"
DECLINED = "policy.decline_self_identification"
USED_DRAFT = "Qwen's draft, used under your policy"


@dataclass(frozen=True)
class Question:
    """What a form field asks, independent of how this form words it."""

    canonical_id: str  # a known meaning, or "q:" plus a fingerprint of the wording
    sensitivity: str  # plain | sensitive
    polarity: str  # positive | negated
    scope: str  # "", us, unspecified, job_location, other:<place>, employer
    known: bool = False
    topic: str = ""  # the sensitive class, when there is one
    default: str = ""  # the policy-default rule that may answer it, when there is one
    detail: str = ""  # rule-specific: sponsorship tense, salary unit
    name: str = ""  # the label as the rules read it
    variant: str = ""  # the wording, when the rule tolerates words that change the question
    answerable: bool = True  # False when the form gives code nothing to go on


def normalized(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def plain_name(label) -> str:
    """The label as a rule reads it; "(Optional)" and "(Required)" are not part of it."""
    return " ".join(re.sub(r"\b(optional|required)\b", " ", normalized(label)).split())


def wording(label) -> str:
    """The label as an unknown question is fingerprinted: every word, qualifiers aside."""
    return normalized(re.sub(r"\((optional|required)\)|\*", " ", str(label or "").lower()))


def _words(text: str) -> frozenset:
    return frozenset(text.split())


# --- sensitive classes -------------------------------------------------------------
# One pattern set decides what only the owner or his approved profile may answer. The
# patterns run on normalized words, so "race" never matches "embrace". A false positive
# costs one question to the owner; a false negative lets a default or a draft answer a
# legal question, so the patterns lean wide.
SENSITIVE_PATTERNS = tuple(
    (topic, re.compile(r"\b(?:" + pattern + r")\b"))
    for topic, pattern in (
        (
            "export_control",
            (
                r"export\w*|itar|ear|sanction\w*|embargo\w*|(?:restricted|denied) part(?:y|ies)|debarred"
                r"|(?:u s|us) persons?|foreign (?:nationals?|persons?)|iran|cuba|north korea|syria|crimea"
            ),
        ),
        ("sponsorship", r"sponsor\w*"),
        (
            "authorization",
            (
                r"authori[sz]\w*|right to work|eligib\w+ to work|eligib\w+ for employment"
                r"|employment eligibility|legally (?:work|able|permitted|entitled|eligible|allowed)"
                r"|lawfully|work permit|permitted to work"
                r"|(?:able|available|allowed|entitled|eligible|free) to (?:legally |lawfully )?work in "
                r"(?:the )?(?:united states|u s a|usa|u s|us|country)"
            ),
        ),
        (
            "visa",
            (
                r"visas?|h ?1 ?b|h1 b|green card|permanent residen\w*|immigration|(?:non ?)?immigrant"
                r"|opt(?! in| out| into)|cpt|ead|i 9|e verify|f ?1|j ?1"
            ),
        ),
        (
            "citizenship",
            r"citizens?|citizenship|nationalit(?:y|ies)|national origin|(?:country|place) of birth",
        ),
        ("clearance", r"clearances?|polygraph|top secret|ts sci|classified"),
        (
            "criminal",
            (
                r"convict\w*|criminal\w*|felon(?:y|ies)|misdemeanou?rs?|arrest\w*|crimes?|plead\w*|pled"
                r"|probation|parole|incarcerat\w*|offen[cs]es?"
            ),
        ),
        (
            "background_check",
            (
                r"background (?:checks?|screen\w*|investigation\w*|verification)"
                r"|drug (?:test\w*|screen\w*)|credit checks?"
            ),
        ),
        ("veteran", r"veterans?|military|armed forces"),
        ("disability", r"disab\w*|accommodations?"),
        (
            "demographics",
            (
                r"gender\w*|sex|race|racial|ethnic\w*|hispanic|latin[oaxe]|sexual orientation|lgbt\w*"
                r"|transgender|pronouns?|self identif\w*|marital status|married|religio\w*|pregnan\w*"
                r"|demographic\w*|eeo\w*|equal (?:employment )?opportunity"
            ),
        ),
        (
            "age",
            (
                r"date of birth|birth ?dates?|birthdays?|dob|years? of age|years old|age|aged"
                r"|(?:18|eighteen) or (?:older|over|above)|(?:over|under|at least) (?:18|eighteen)"
            ),
        ),
        (
            "identity_document",
            (
                r"social security|ssn|passports?|driver s licen[cs]e|bank account|routing number"
                r"|tax id\w*|national id\w*"
            ),
        ),
        (
            "salary",
            (
                r"salar(?:y|ies)|compensation|wages?|remuneration|stipend|hourly rate|rate of pay"
                r"|(?:desired|expected) pay|pay (?:rate|range|expectations?|requirements?)"
            ),
        ),
        (
            "agreement",
            (
                r"certify|certifies|i hereby|attest\w*|affirm\w*|swear|agree\w*|consent\w*|signature\w*"
                r"|e sign\w*|signed|to sign|sign (?:here|below)|acknowledg\w*|privacy"
                r"|terms (?:and conditions|conditions|of (?:use|service|employment))"
                r"|non ?compete|non ?disclosure|nda|arbitrat\w*|binding|truthful\w*|true and"
                r"|perjury|misrepresent\w*|falsif\w*|waive\w*|release of information"
            ),
        ),
    )
)

# Voluntary self-identification: the questions a form's own decline option may answer.
SELF_ID = re.compile(
    r"\b(?:gender\w*|sex|race|racial|ethnic\w*|hispanic|latin[oaxe]|veterans?|disab\w*"
    r"|sexual orientation|lgbt\w*|transgender|self identif\w*)\b"
)
DECLINE_OPTION = re.compile(
    r"decline|prefer not|don.?t wish|do not wish|don.?t want to|do not want to|"
    r"not to (answer|disclose|self)|choose not|rather not",
    re.IGNORECASE,
)

# A profile policy value that keeps a class with the owner on every form.
ASK_EACH_TIME = "ask_each_time"
POLICY_KEYS = {
    "export_control": ("eligibility", "export_control_questions"),
    "demographics": ("application_policy", "demographics"),
    "disability": ("application_policy", "disability"),
    "veteran": ("application_policy", "veteran_status"),
    "salary": ("application_policy", "salary_questions"),
}


# "Sponsor" is the authorization question only next to visa, work or employment words, or
# when something requires or needs it: "sponsored hackathons" is a plain question.
SPONSOR_CONTEXT = re.compile(
    r"\b(?:visas?|immigra\w*|work(?:s|ing)?|employ\w*|authori[sz]\w*|permits?|status|h ?1 ?b"
    r"|green card"
    r"|citizen\w*|residen\w*|legal\w*|lawful\w*|opt|cpt|stem|countr\w*|nationals?|foreign"
    r"|united states|u s a?|usa|us|transfer\w*|petition\w*)\b"
    r"|\b(?:require|requires|required|requiring|need|needs|needed|needing)\b(?: [a-z0-9]+){0,4}"
    r" sponsor\w*|\bsponsor\w* (?:is |be |are )?(?:required|needed)\b"
)


def sensitive_topic(name: str) -> str:
    for topic, pattern in SENSITIVE_PATTERNS:
        if pattern.search(name) and (topic != "sponsorship" or SPONSOR_CONTEXT.search(name)):
            return topic
    return ""


# A field that asks for an identity document, a bank detail or a one-time code is a step
# the owner takes in the browser himself. The intent must be the field's own: "Name as it
# appears on your passport" asks for a name, not for the passport.
MANUAL_ONLY = re.compile(
    r"\b(?:passport (?:number|no|num|id|document|documents|copy|scan|image|photo|page|details)"
    r"|(?:copy|scan|image|photo|picture|upload) of (?:your |the )?passport"
    r"|ssn|social security (?:number|no|card)|social insurance number"
    r"|bank account|routing number|verification code"
    r"|drivers? (?:s )?licen[cs]e (?:number|no|id))\b"
)
MANUAL_ONLY_LABELS = frozenset({"passport", "your passport", "social security", "ssn"})


def manual_only(label) -> bool:
    """Whether a field asks for something only the owner may enter, by hand."""
    name = plain_name(label)
    return bool(MANUAL_ONLY.search(name)) or name in MANUAL_ONLY_LABELS


# A select's own prompt ("Select...", "-- Choose --") is not an answer anyone can give.
PLACEHOLDER_OPTION = re.compile(
    r"(?:please )?(?:select|choose|pick)(?: (?:one|an? (?:option|answer|item|value)"
    r"|from (?:the )?list|below|here|(?:a|an|your|the) [a-z]+|[a-z]+))?"
)


def placeholder_option(label) -> bool:
    text = normalized(label)
    return not text or bool(PLACEHOLDER_OPTION.fullmatch(text))


def real_options(options) -> list[str]:
    """The option labels a person could choose: a select's placeholder is left out."""
    return option_labels(options)


# --- places -------------------------------------------------------------------------
US = r"(?:the )?(?:united states(?: of america)?|u s a|usa|u s|us)"
US_NAMED = re.compile(r"\b(?:united states(?: of america)?|u s a|usa|u s|us)\b")
# "us" is the country only where the words around it say so; otherwise it is the employer.
US_COUNTRY_CONTEXT = re.compile(
    r"\b(?:united states(?: of america)?|u s a|usa|u s)\b"
    r"|\b(?:in|within|inside|outside|outside of) (?:the )?us\b|\bthe us\b"
    r"|\bus (?:citizen\w*|persons?|nationals?|work|based|resident\w*|government|military|visa"
    r"|export|laws?|security|territor\w*|employment|veterans?)\b"
)
COUNTRY_ALIASES = {"united states", "united states of america", "usa", "us", "u s", "u s a"}

# Countries and regions other than the United States. A sponsorship, authorization or
# citizenship question that names one is a different question from the US one. Names that
# are also US states or common first names (Georgia, Jordan, Chad) are left out: a legal
# rule rejects any word it does not know anyway.
OTHER_PLACES = (
    "afghanistan|albania|algeria|andorra|angola|argentina|armenia|australia|austria|azerbaijan"
    "|bahamas|bahrain|bangladesh|barbados|belarus|belgium|belize|benin|bhutan|bolivia|bosnia"
    "|botswana|brazil|brunei|bulgaria|burkina faso|burundi|cambodia|cameroon|canada|chile|china"
    "|colombia|costa rica|croatia|cyprus|czech republic|czechia|denmark|dominican republic"
    "|ecuador|egypt|el salvador|estonia|ethiopia|fiji|finland|france|gabon|gambia|germany|ghana"
    "|greece|guatemala|guinea|guyana|haiti|honduras|hong kong|hungary|iceland|india|indonesia"
    "|iraq|ireland|israel|italy|ivory coast|jamaica|japan|kazakhstan|kenya|kuwait|kyrgyzstan"
    "|laos|latvia|lebanon|liberia|libya|liechtenstein|lithuania|luxembourg|madagascar|malawi"
    "|malaysia|maldives|mali|malta|mauritius|mexico|moldova|monaco|mongolia|montenegro|morocco"
    "|mozambique|myanmar|namibia|nepal|netherlands|new zealand|nicaragua|niger|nigeria"
    "|north macedonia|norway|oman|pakistan|panama|paraguay|peru|philippines|poland|portugal"
    "|puerto rico|qatar|romania|russia|rwanda|saudi arabia|senegal|serbia|singapore|slovakia"
    "|slovenia|somalia|south africa|south korea|korea|spain|sri lanka|sudan|sweden|switzerland"
    "|taiwan|tajikistan|tanzania|thailand|tunisia|turkey|turkiye|uganda|ukraine"
    "|united arab emirates|uae|united kingdom|uk|great britain|britain|england|scotland|wales"
    "|northern ireland|uruguay|uzbekistan|venezuela|vietnam|yemen|zambia|zimbabwe"
    "|iran|cuba|north korea|syria|crimea"
    "|europe|european union|eu|eea|emea|apac|asia|africa|latin america|latam|south america"
    "|north america|middle east|oceania"
    "|another country|other countr(?:y|ies)|any other country|(?:your |my )?home country"
    "|(?:your |my )?country of (?:residence|origin|citizenship)|abroad|overseas|internationally"
)
OTHER_PLACE = re.compile(r"\b(?:" + OTHER_PLACES + r")\b")
JOB_PLACE = re.compile(
    r"\b(?:(?:the|this) (?:country|location|region|jurisdiction)"
    r"(?: (?:in which|where|that) (?:this|the) (?:job|role|position|opportunity|internship) "
    r"is (?:located|based)| (?:in which|where) (?:you are|i am) applying(?: to work)?"
    r"| of employment)?"
    r"|(?:the|your) country of employment"
    r"|the location (?:of|for) this (?:job|role|position))\b"
)
WHERE = rf"(?:{US}|xplace|xjobplace)"


def with_places(name: str) -> tuple[str, list[str]]:
    """The name with non-US places replaced by a placeholder, and the places found."""
    text = name.replace("new mexico", "newmexico")
    found = [m.group(0) for m in OTHER_PLACE.finditer(text)]
    text = OTHER_PLACE.sub("xplace", text)
    text = JOB_PLACE.sub("xjobplace", text)
    return text, found


def place_scope(text: str, found: list[str]) -> str:
    if "xplace" in text.split():
        return "other:" + "+".join(sorted(set(found)))
    if "xjobplace" in text.split():
        return "job_location"
    return "us" if US_NAMED.search(text) else "unspecified"


# --- negation -----------------------------------------------------------------------
NEGATION_WORDS = _words("not no never without neither nor none cannot outside except unless")
ANTONYMS = {
    "unwilling": "willing",
    "unable": "able",
    "unavailable": "available",
    "uncomfortable": "comfortable",
    "ineligible": "eligible",
    "unauthorized": "authorized",
    "unauthorised": "authorised",
}
CONTRACTION = re.compile(
    r"\b(are|is|do|does|did|will|would|can|could|have|has|had|should|were|was)n t\b"
)


def affirmative(name: str) -> tuple[str, bool]:
    """The name with its negations removed, and whether there were any."""
    text = re.sub(r"\byes (?:or )?no\b|\by n\b", " ", name)
    text = CONTRACTION.sub(r"\1 not", text)
    text = re.sub(r"\bwon t\b", "will not", text)
    text = re.sub(r"\bcan t\b", "can not", text)
    words, negated = [], False
    for word in text.split():
        if word in NEGATION_WORDS:
            negated = True
        elif word in ANTONYMS:
            negated = True
            words.append(ANTONYMS[word])
        else:
            words.append(word)
    return " ".join(words), negated


# --- canonical rules ----------------------------------------------------------------
IDENTITY_NAMES = {
    "first_name": "first name|legal first name|first name legal|given name|legal given name",
    "middle_name": "middle name|legal middle name",
    "last_name": "last name|legal last name|last name legal|family name|surname",
    "preferred_name": "preferred name|preferred first name",
    "full_name": "name|full name|legal name|legal full name|full legal name|your name"
    "|your full name|name as it appears on your passport|full name as it appears on your passport"
    "|legal name as it appears on your passport|full legal name as it appears on your passport"
    "|name as it appears on passport|name as shown on your passport",
    # The name in the applicant's own script: the legal name itself when it is Latin.
    "full_name_native": "full legal name in native language|full name in native language"
    "|legal name in native language|name in native language|native language name"
    "|full name in your native language|name in your native language"
    "|full legal name in your native language|legal full name in native language"
    "|full legal name in native language if applicable|name in native language if applicable",
    "email": "email|email address|e mail|e mail address",
    "phone": "phone|phone number|mobile phone|mobile number|cell phone|mobile phone number"
    "|cell phone number|telephone|telephone number|phone number including country code"
    "|phone no|mobile no",
    "phone_type": "contact phone type|phone type|phone number type",
    "location_city": "city|location city|current city",
    "state_region": "state|state region|state province|state province region",
    "country": "country|country region|country region of residence|country of residence"
    "|current country of residence|country of current residence|what is your country of residence"
    "|what country do you live in|which country do you live in|what country do you currently live in"
    "|which country do you currently live in|which country do you currently reside in"
    "|what country do you currently reside in|country you reside in|country where you reside"
    "|residence country",
    "postal_code": "zip code|postal code|zip|zip postal code",
    "location": "location|current location|city state|city and state|where are you located"
    "|where are you currently located|where are you based|your location|your current location"
    "|where do you live|where do you currently live|current city and state"
    "|what is your current location|what is your location",
    "linkedin_url": "linkedin|linkedin profile|linkedin url|linkedin profile url",
    "github_url": "github|github url|github profile",
    "portfolio_url": "portfolio|website|personal website|portfolio url",
    # The school applications state (education.py), in an education block or outside one.
    "school": "school|university|college university|college|school name|name of school"
    "|university name|name of university|college name|school university|university college"
    "|school or university|college or university|university or college|institution"
    "|educational institution|name of institution",
    # Where the owner is enrolled today: the entry whose dates include today, which may
    # not be the one applications state.
    "current_school": "current school|current university|current college|current institution"
    "|current school name|name of current school|what school do you attend"
    "|what school do you currently attend|which school do you attend"
    "|which school do you currently attend|what university do you attend"
    "|which university do you attend|what university do you currently attend"
    "|which university do you currently attend|what college do you attend"
    "|which college do you attend|what college do you currently attend"
    "|which school are you attending this semester|which school are you currently attending"
    "|what school are you currently attending|where are you currently enrolled"
    "|where do you currently go to school|school you currently attend"
    "|name of the school you currently attend|currently enrolled school",
    "enrollment_status": "enrollment status|current enrollment status|what is your enrollment status"
    "|what is your current enrollment status|student status|current student status"
    "|are you currently enrolled|are you currently a student|are you a current student"
    "|are you currently enrolled in school|are you currently enrolled in college"
    "|are you currently enrolled in a university|are you currently enrolled in a college"
    "|are you currently enrolled in a degree program|are you currently pursuing a degree"
    "|are you enrolled in a degree program|are you currently enrolled in a university or college",
    "class_standing": "class standing|current class standing|what is your class standing"
    "|what is your current class standing|year in school|current year in school"
    "|what is your current year in school|what year are you in school|what year are you in"
    "|academic year|current academic year|what is your current academic year"
    "|class level|year of study|current year of study|what is your year of study"
    "|student year|current student year",
    "major": "major|field of study|what is your field of study|discipline|area of study"
    "|major field of study|major discipline|major field|academic major|primary major|your major"
    "|what is your major|degree major|course of study|program of study|major area of study"
    "|field of study major|major field of study discipline",
    "degree": "degree|degree type|type of degree|degree level|degree program|current degree"
    "|degree level currently pursuing|degree currently pursuing|current degree level"
    "|level of degree|level of degree currently pursuing|degree level pursuing"
    "|what degree level are you currently pursuing|current degree program"
    "|degree in progress|what degree are you pursuing|what degree are you currently pursuing"
    "|degree you are pursuing|degree pursuing",
    "sex": "sex",
    "sexual_orientation": "sexual orientation|what is your sexual orientation",
    "citizenship_country": "citizenship|country of citizenship|what is your country of citizenship"
    "|nationality|what is your nationality",
    "how_did_you_hear": "source|referral source|application source|how did you find us",
}
# Common questions with one meaning however a form words them, none an identity fact.
# The warm-up in `#memory` (common_questions.py) asks each once; the answer then fills
# every wording here. Bare "end date" or "languages" are left out on purpose: in a work
# history or on a developer form they mean something else.
COMMON_NAMES = {
    "street_address": "address|street address|address line 1|street address line 1|home address"
    "|mailing address|current address|residential address|address 1",
    "languages_spoken": "languages spoken|spoken languages|what languages do you speak"
    "|which languages do you speak|languages you speak|what languages are you fluent in"
    "|which languages are you fluent in|fluent languages|languages you are fluent in",
    "work_style_preference": "preferred work arrangement|work arrangement preference"
    "|what is your preferred work arrangement|preferred work style|work style preference"
    "|preferred work setting|which work setting do you prefer|preferred work environment"
    "|work location preference|preferred work location type|work mode|preferred work mode"
    "|preferred working arrangement|remote hybrid or on site|remote hybrid or onsite"
    "|do you prefer remote hybrid or on site|do you prefer remote hybrid or onsite"
    "|are you looking for remote hybrid or on site work|remote or on site|remote or onsite",
    # When the owner started at the school he attends now (the warm-up asks it once).
    "school_start": "enrollment date|enrollment start date|date of enrollment"
    "|when did you start school|when did you start at your school"
    "|when did you start at your current school|when did you start college"
    "|when did you start university|current school start date",
    # When the degree applications state begins, which may lie ahead.
    "degree_start": "degree start date|start date of your degree|when did you begin your degree"
    "|school start date|school start month|college start date|university start date",
    # Who the owner has worked for, as a list in his own words: it answers "have you
    # worked for us before" at every employer that is not on it.
    "past_employers": "past employers|previous employers|former employers"
    "|which companies have you worked for|companies you have worked for"
    "|which companies have you worked for as an employee intern or contractor"
    "|list your previous employers|list your past employers",
    "preferred_locations": "preferred location|preferred locations|preferred work location"
    "|preferred work locations|preferred office location|preferred office locations"
    "|preferred office|which office location do you prefer|location preference"
    "|location preferences|what is your preferred location|what are your preferred locations"
    "|where would you like to work|preferred city|preferred cities",
}
EXACT_NAMES = {
    name: key
    for key, names in (*IDENTITY_NAMES.items(), *COMMON_NAMES.items())
    for name in names.split("|")
}
PLACE_LABELS = frozenset(
    {"location", "current location", "location city", "city", "city state", "where are you located"}
)

# A label that contains a fact's name means that fact only when every other word is a
# neutral qualifier: "LinkedIn Profile URL" is LinkedIn, "Referrer email" is nobody's.
NEEDLES = (
    ("linkedin", "linkedin_url"),
    ("github", "github_url"),
    ("portfolio", "portfolio_url"),
    ("personal website", "portfolio_url"),
    ("personal site", "portfolio_url"),
    ("website", "portfolio_url"),
    ("profile link", "portfolio_url"),
    ("profile url", "portfolio_url"),
    ("online profile", "portfolio_url"),
    ("given name", "first_name"),
    ("first name", "first_name"),
    ("family name", "last_name"),
    ("surname", "last_name"),
    ("last name", "last_name"),
    ("e mail", "email"),
    ("email", "email"),
    ("mobile", "phone"),
    ("phone", "phone"),
    ("zip", "postal_code"),
    ("postal", "postal_code"),
)
QUALIFIERS = _words(
    "your my the a an please enter provide add share current personal primary best contact"
    " number address url link profile account page of for to legal code zip postal if any"
    " applicable available you have one what is confirm again re candidate applicant s"
)

AUTHORIZED = re.compile(
    r"(?:(?:are|will) you (?:be )?(?:(?:currently|presently|legally|lawfully|work) )*"
    r"(?:authori[sz]ed|eligible|permitted|allowed|entitled|(?:legally|lawfully) able)"
    r"|do you (?:currently |presently )?have (?:the )?(?:(?:legal|lawful) )?"
    r"(?:right|authori[sz]ation|permission|eligibility)"
    r"|(?:legally |lawfully )?authori[sz]ed)"
    r" to (?:(?:legally|lawfully) )?(?:work|be employed)(?: (?:legally|lawfully))?"
    r"(?: for (?:any|an|a|the|this|our) (?:employer|company|organization))?"
    rf"(?: in (?P<where>{WHERE}))?(?P<tail>(?: [a-z0-9]+)*)"
)
AUTHORIZED_TAIL = _words(
    "for any an a the this our employer company organization role position job on full time"
    " basis at currently now legally lawfully"
)
WITHOUT_SPONSORSHIP = re.compile(
    r" without (?:the need for |needing |requiring |any (?:need for )?)?"
    r"(?:(?:visa|employer|company|immigration|employment|work) )*sponsorship"
    r"(?: now or in the future)?"
)
SPONSORSHIP_WORDS = _words(
    "will do would you now or in the future currently presently ever either at any time point"
    " require need an a our company employer employment based visa visas immigration work"
    " working authorization permit h1b h1 h 1b 1 b status sponsorship for to of e g i such as"
    " example like legally lawfully continue be employed remain stay order obtain maintain"
    " extend extension type kind form related support opt cpt stem f1 f j1 j tn l1 l o1 o e3"
    " lawful legal from this your us u s usa united states america xplace xjobplace order"
)
# A condition in front of the sponsorship question that does not change it: "If working in
# the US, will you now or in the future require sponsorship?"
LEADING_CONDITION = re.compile(
    r"if (?:you are |you were |you re |i am |i were )?(?:working|employed|hired|based|located"
    r"|living|selected|offered(?: (?:employment|the (?:job|role|position)|a position|this"
    r" (?:job|role|position)))?)(?: for (?:this|the) (?:job|role|position))?"
    rf"(?: (?:in|within) (?:{US}|xplace|xjobplace))? "
)
CITIZEN = re.compile(
    rf"(?:(?:are you|i am) (?:currently )?an? )?(?:(?:{US}|xplace) citizen|citizen of (?:{US}|xplace))"
)
ADULT = re.compile(
    r"are you (?:(?:at least |over |above )?(?:18|eighteen)(?: years)?(?: of age| old)?"
    r"(?: or (?:older|over|above))?|(?:over|above|at least) the age of (?:18|eighteen))"
)
CLEARANCE = re.compile(
    r"do you (?:currently |presently )?(?:have|hold|possess) (?:an? |any )?"
    rf"(?:(?:active|current|valid) )*(?:{US} )?(?:government )?security clearance"
)
FELONY = re.compile(r"have you ever been convicted of a felony")
CRIME = re.compile(r"have you ever been convicted of (?:a|any) (?:crime|criminal offen[cs]e)")

# Vocabulary rules: (canonical id, words one of which must appear, every allowed word).
VOCABULARY_RULES = (
    (
        "gender",
        "gender",
        (
            "gender identity what is your my please select choose indicate i identify as do you"
            " with which how would describe voluntary self identification the of to"
        ),
    ),
    (
        "hispanic_latino",
        "hispanic latino latina latinx",
        "are you hispanic latino latina latinx or of origin ethnicity do identify as a i am",
    ),
    (
        "veteran_status",
        "veteran",
        (
            "veteran status protected are you a an what is your my please select identify i as do"
            " us u s military of the armed forces self identification voluntary indicate"
        ),
    ),
    (
        "disability_status",
        "disability disabilities",
        (
            "disability disabilities status do you have a an or had ever history record of one"
            " please select indicate i identify as having voluntary self identification what is"
            " your my with person are individual in the past currently previously"
        ),
    ),
    (
        "pronouns",
        "pronouns pronoun",
        "pronouns pronoun preferred your my what are personal please select share gender",
    ),
)
# Race and ethnicity are two questions on many forms and one on others: three ids.
RACE_WORDS = _words(
    "race ethnicity ethnic racial background origin group category what is your my please"
    " select identify indicate i as do you how would describe and or the which best"
    " describes voluntary self identification"
)
TRUTHFUL_WORDS = _words(
    "i hereby certify attest affirm confirm declare acknowledge that the all information"
    " answers statements provided given submitted in on this my application form resume is are"
    " true complete correct accurate truthful and to best of knowledge above herein by"
    " checking box submitting signing below have has been understand any false misleading"
    " misrepresentation omission falsification may be grounds for result rejection dismissal"
    " termination employment discharge refusal hire withdrawal offer disqualification or if"
    " employed cause immediate lead omissions from consideration"
)
PRIVACY_WORDS = _words(
    "i have read and agree to the our privacy policy notice statement consent acknowledge"
    " accept understand understood reviewed by checking this box that processing of my"
    " personal data information in accordance with as described do you"
    " application will be processed handled collected used candidate applicant recruitment"
)
# The employer's own name in "…in accordance with <employer>'s Candidate Privacy Policy".
PRIVACY_OWNER = re.compile(
    r"\bwith (?:[a-z0-9]+ ){1,6}?(?=(?:candidate |applicant |recruitment )?privacy\b)"
)
SALARY_WORDS = _words(
    "salary compensation pay expectation expectations desired expected what is are your annual"
    " yearly base range requirement requirements for this role position in usd please provide"
    " hourly rate per hour year target"
)
CONTACT_NOUNS = _words(
    "contact contacted text texts sms message messages call calls email emails communications"
)
CONTACT_VERBS = _words("agree agreement consent opt may can allow like ok okay willing happy")
# The employer's own name in "…updates from <employer> regarding your application".
CONTACT_SENDER = re.compile(r"\bfrom (?:[a-z0-9]+ ){1,8}?(?=(?:regarding|about|concerning)\b)")
CONTACT_WORDS = _words(
    "i do you agree consent to be being contacted contact receive receiving by via text texts"
    " sms message messages messaging phone call calls email emails e mail from the company"
    " recruiter recruiters recruiting team hiring our us about regarding this my your"
    " application job opportunity opportunities future openings roles positions status updates"
    " interview scheduling and or can may we me at number provided above would like is it ok"
    " okay opt in for communications related employment allow yes please send reach out mobile"
    " data rates apply talent network community pool join joining added add keep kept"
    " considered consider other notified notify are willing happy"
    " agreement check indicate no of frequency vary stop reply out view here privacy policy"
    " terms conditions"
)
# "I authorize the <employer's team> to consider me for other roles …": the whole label is
# that one request. The team's name may be any words but a second request ("to run a
# check and …"), and nothing after it may ask for another consent.
CONSIDER_OTHER = re.compile(
    r"(?:i (?:authorize|allow|agree|consent|would like|want)|please allow|do you (?:want|agree"
    r"|consent|authorize)|would you like|may)"
    r"(?: (?:for )?(?!and\b|to\b|or\b)[a-z0-9]+){0,8}? to (?:be )?consider(?:ed)?"
    r"(?: (?:me|you|my (?:application|profile|resume|candidacy)))? for "
    r"(?:(?:other|future|additional|similar|any) )+(?:[a-z]+ ){0,2}?"
    r"(?:opportunities|roles|positions|openings|jobs)"
    r"(?: (?!and\b|agree|authori|consent|certif|waiv|releas|acknowledg)[a-z0-9]+){0,18}"
)
MARKETING_WORDS = CONTACT_WORDS | _words(
    "marketing newsletter newsletters promotional promotions news offers product products"
    " subscribe events"
)

HEARD = re.compile(
    r"(?:how|where) did you (?:first )?(?:(?:hear|learn|find out) (?:about|of)"
    r"|find(?= (?:us|this|our)\b)|come across)(?: [a-z0-9]+){1,8}"
)
READ_POSTING = re.compile(
    r"(?:have you|i have|i acknowledge that i have|i confirm that i have) read"
    r"(?: and (?:understood|understand))? the (?:(?:full|entire|complete) )?"
    r"(?:job|role|position) (?:description|posting|requirements|duties)"
    r"(?: and (?:requirements|responsibilities|qualifications))?"
    r"(?: for this (?:job|role|position)| above| in full| carefully)?"
)
START_DATE = re.compile(
    r"(?:when|what date) (?:can|could|would) you (?:be able to )?(?:start|begin)(?: work(?:ing)?)?"
    r"|when are you available to (?:start|begin)(?: work(?:ing)?)?"
    r"|(?:what is your )?(?:(?:earliest|available|desired|preferred|anticipated) )*"
    r"(?:possible )?start date|date available(?: to start)?|available start date"
)
# The same availability asked as a month: its own question, so a bare "June" the owner
# gives for it never fills a start-date box.
START_MONTH = re.compile(
    r"(?:what is your )?(?:(?:earliest|available|desired|preferred|anticipated) )*"
    r"(?:possible )?start month"
    r"|(?:which|what) month (?:can|could|would) you (?:be able to )?(?:start|begin)(?: work(?:ing)?)?"
)
# The cumulative GPA however a form words it. A major GPA, a high-school GPA or a graduate
# GPA is another number; a stated scale must be the profile's own.
GPA = re.compile(
    r"(?:what is your |please (?:enter|provide|list) your |your )?"
    r"(?:(?:current|cumulative|overall|undergraduate|college|university|unweighted|latest"
    r"|most recent) )*(?:gpa|grade point average)(?: (?:cumulative|undergraduate|overall))?"
    r"(?: (?:on a |out of |on |scale |of )?(?P<scale>4 0|4|5 0|5|10|100)(?: point)?(?: scale)?)?"
    r"(?: if applicable)?"
)
GRADUATION_DATE = frozenset(
    {
        "when is your expected graduation date month year",
        "expected graduation date",
        "anticipated graduation date",
        "graduation date",
        "expected graduation",
        "what is your expected graduation date",
        "what is your graduation date",
        "when do you expect to graduate",
        "when will you graduate",
    }
)
GRADUATION_YEAR = frozenset({"graduation year", "expected graduation year", "year of graduation"})
LOCATED_IN_US = re.compile(
    rf"are you (?:currently )?(?:located|based|residing|living)(?: and (?:located|based))? in {US}"
)
RELOCATION_HELP = re.compile(
    r"(?:will|do|would) you (?:require|need) relocation (?:assistance|support)"
    r"(?: for this (?:job|role|position))?"
)
WORKED_HERE = re.compile(
    r"(?:do you currently or )?have you (?:(?:ever|previously|already) )*"
    r"(?:worked|been employed|been an employee|interned)(?: (?:previously|before|in the past))?"
    r"(?: (?P<prep>for|at|by|with) (?P<rest>[a-z0-9]+(?: [a-z0-9]+)*)"
    r"| here(?: (?:before|previously|in the past))?)"
)
# What may follow the employer's name in that question without changing it.
WORKED_BOUNDARY = _words("as before previously prior in or and during either since note")
WORKED_TAIL = WORKED_BOUNDARY | _words(
    "an a employee employees intern interns internship contractor contractors consultant"
    " consultants full time part fulltime parttime the past ever temporary temp worker"
    " contingent any capacity role that providing false misleading inaccurate incorrect"
    " information may result disqualification from hiring process this application"
)
# "us", "this company": the employer whose form it is.
EMPLOYER_SELF = frozenset(
    {
        "us",
        "our company",
        "this company",
        "the company",
        "our organization",
        "this organization",
        "our firm",
        "this firm",
        "this employer",
        "our team",
    }
)
CORPORATE_SUFFIX = _words("inc incorporated llc ltd limited corp corporation co plc gmbh")


def worked_here_employer(text: str) -> list[str] | None:
    """The employer a "have you worked for ..." label names, as words; [] for "us" or
    "here", None when the label is not that question or adds a condition."""
    match = WORKED_HERE.fullmatch(text)
    if not match:
        return None
    if match["rest"] is None:
        return []  # "have you worked here before"
    words = match["rest"].split()
    cut = next((i for i, w in enumerate(words) if w in WORKED_BOUNDARY), len(words))
    company, tail = words[:cut], words[cut:]
    if not 1 <= len(company) <= 6 or not set(tail) <= WORKED_TAIL:
        return None
    if " ".join(company) in EMPLOYER_SELF:
        return []
    if match["prep"] == "with":
        return None  # "worked with React" is about a tool, not an employer
    while company and company[-1] in CORPORATE_SUFFIX:
        company = company[:-1]
    return company or None


ENROLLED_AT = re.compile(
    r"are you (?:currently |presently |now )?(?:enrolled|a student|studying|attending)"
    r" (?:at|in) (?P<school>[a-z0-9]+(?: [a-z0-9]+){0,7})"
)
# Words that make "enrolled in ..." about any school rather than one named school.
ENROLLED_FILLER = _words(
    "a an the school college university program degree undergraduate accredited course of study"
)
# A kind of program the answer depends on ("a bachelor's program"): not a yes for any
# school, so such a question is left alone.
ENROLLED_KIND = _words("graduate bachelor bachelors s master masters four 4 two 2 year full part")


# Ties to the employer's people and earlier contact with it. Each is the owner's general
# answer (given once in the warm-up) unless he answered for this employer.
SOME = r"(?: [a-z0-9]+){0,6}"
RELATED_HERE = re.compile(
    r"(?:are you|is anyone in your (?:immediate )?family) related to (?:any|anyone|someone|a|an)"
    rf"{SOME} (?:employees?|staff|team members?|people|person|individuals?)"
    rf"(?: (?:at|of|for|with|who works? (?:at|for)|working (?:at|for))(?: [a-z0-9]+){{1,6}})?"
    r"|do you have (?:any )?(?:relatives|family members?|family)"
    rf"{SOME} (?:who (?:is|are) )?(?:currently )?(?:employed|working|work|works)"
    r"(?: (?:at|by|for|with)(?: [a-z0-9]+){1,6})?"
)
KNOWS_HERE = re.compile(
    r"do you (?:personally )?know (?:any|anyone|someone|any of the)"
    rf"{SOME} (?:employees?|staff|people|team members?"
    r"|who (?:currently )?works?(?: (?:at|for|here))?)"
    r"(?: (?:at|of|for|with|here)(?: [a-z0-9]+){0,6})?"
)
REFERRED_HERE = re.compile(
    rf"(?:were|have) you (?:been )?referred{SOME} by (?:a|an|any|someone|anyone){SOME}"
    rf"|did (?:a|an|any|someone|anyone){SOME} refer you{SOME}"
)
INTERVIEWED_HERE = re.compile(
    r"have you (?:(?:ever|previously|already) )*interviewed"
    r"(?: (?:previously|before|in the past))? (?:with|at|for)(?: [a-z0-9]+){1,6}"
)
# "U.S. Person" as export rules define it: a citizen, a permanent resident, a refugee or
# an asylee. One question however a form words its definition.
US_PERSON = re.compile(r"\b(?:u s|us) persons?\b")
US_PERSON_WORDS = _words(
    "are you a an u s us person persons as defined by under the in itar ear export control"
    " administration regulations regulation international traffic arms 22 cfr 120 62 15 772"
    " i am do you qualify meaning of which includes means include including citizen citizens"
    " national nationals lawful lawfully admitted permanent resident residents green card"
    " holder holders refugee refugees asylee asylees protected individual individuals"
    " granted asylum status or and of for this position purposes is be considered who"
    " definition e g i e that 8 usc 1324b a 3 united states america in"
)


# A form may state the definition first and then ask to confirm one of its statuses.
US_PERSON_CONFIRM = re.compile(
    r"(?:please )?(?:confirm|indicate|state|select|tell us) (?:whether|if|that) you "
    r"(?:fall (?:into|within|under)|meet|satisfy|are in|hold|have) "
    r"(?:at least )?(?:one|any) of (?:the|these) (?:(?:three|3|above|following|listed) )*"
    r"(?:statuses|status|criteria|categories)(?: (?:above|listed|below))*$"
)
US_PERSON_DENIALS = _words("not non no neither nor none foreign unless except without")


def us_person_asked(text: str, words: list[str]) -> bool:
    """Whether a label that names "U.S. Person" asks if the applicant is one: the plain
    question in the rule's own words, or its definition (which lists a citizen) followed
    by a request to confirm one of its statuses. Any denial or exception in the wording
    makes it another question, which is the owner's."""
    if re.match(r"(?:are you|i am|do you qualify)\b", text) and set(words) <= US_PERSON_WORDS:
        return True
    return (
        bool(US_PERSON_CONFIRM.search(text))
        and "citizen" in words
        and not set(words) & US_PERSON_DENIALS
    )


APPLIED_HERE = re.compile(
    r"have you (?:(?:ever|previously) )*applied(?: previously| before| in the past)?"
    r" (?:to|at|with|for)(?: [a-z0-9]+){1,6}"
)
EMPLOYER_TIES = (
    ("related_to_employee", RELATED_HERE),
    ("knows_employee", KNOWS_HERE),
    ("referred_by_employee", REFERRED_HERE),
    ("previously_interviewed_here", INTERVIEWED_HERE),
    ("previously_applied_here", APPLIED_HERE),
)
GENERAL_NO_IDS = frozenset(canonical for canonical, _pattern in EMPLOYER_TIES)


def general_no(question: "Question", value) -> bool:
    """Whether an answer about one employer holds for any employer: a plain no to a tie
    with it (a relative there, someone he knows, a referral, an earlier interview or
    application). A yes is about that employer alone."""
    return (
        question.canonical_id in GENERAL_NO_IDS
        and question.polarity == POSITIVE
        and (normalized(value).split() or [""])[0] in {"no", "none", "never"}
    )


WILLING = (
    r"(?:(?:are|would|will) you (?:be )?|i am )(?:(?:currently|also) )?"
    r"(?:willing|able|open|prepared|comfortable|okay|ok|happy|available)"
    r"(?: and (?:willing|able))?"
)
WILLINGNESS = re.compile(
    rf"(?:{WILLING} (?:to|with|for|working|relocating|commuting)|"
    r"can you (?:work|commute|come|be|start|relocate|travel|attend)|"
    r"willing(?:ness)? to (?:relocate|work|commute|travel)|open to relocation)\b"
)
RELOCATE = re.compile(r"\brelocat\w*\b")
# What an internship form asks about the term after it: whether he goes back to school.
RETURNING = re.compile(
    r"(?:do you (?:intend|plan|expect) to|will you(?: be)?|are you (?:planning|intending|going) to)"
    r" (?:return(?:ing)?|go(?:ing)? back) to (?:school|college|university|your studies"
    r"|(?:a |an |your )?(?:(?:degree seeking|academic|degree|undergraduate|graduate) )*program)\b"
)
ONSITE = re.compile(r"\b(?:on ?site|in person|hybrid|office|offices)\b")
STACK = re.compile(rf"{WILLING} (?:working |coding |programming |developing )?(?:with|in|using)\b")
# Only a stated willingness takes the general default; "are you able to" claims an ability.
STATED_WILLINGNESS = re.compile(
    r"(?:(?:are|would|will) you (?:be )?|i am )(?:(?:currently|also) )?"
    r"(?:willing|comfortable|open|okay|ok|happy|prepared)\b"
)
# Words that turn a willingness question into something "yes" does not safely answer.
RISK_WORDS = _words(
    "assistance expense expenses cost costs unpaid free volunteer equity commission deferred fee"
    " fees deposit bond purchase invest pay paid cut own lower less reduced reduction restrict"
    " restriction restrictions restricted prevent prevents conflict conflicts limitation"
    " limitations immediately immediate asap within today tomorrow xplace"
)
EMPLOYER_RELATIVE = re.compile(
    r"\b(?:here|us|our|this (?:company|organization|firm|employer|role|position|job|opportunity"
    r"|internship|team|program)|the (?:company|role|position))\b"
)
# More common questions (see COMMON_NAMES), matched whole. Each names what it asks:
# a bare "end date" or "references" field is never taken for one of these.
HOURS_PER_WEEK = re.compile(
    r"(?:(?:how many |what )?hours (?:per|a|each) week(?: (?:of )?availab\w*)?"
    r"(?: (?:are|can|could|would|will) you"
    r" (?:be )?(?:available|work|working|commit|dedicate)(?: to (?:work|working))?)?"
    r"|(?:how many|what) hours (?:are|can|could|would|will) you (?:be )?"
    r"(?:available|work|working|commit)(?: to work)? (?:per|a|each) week"
    r"|(?:desired|preferred|expected|available|weekly) hours(?: (?:per|a|each) week)?"
    r"|availability (?:in )?hours (?:per|a|each) week|hours of availability (?:per|a|each) week"
    r"|number of hours (?:per|a|each) week(?: (?:you are |you re )?available)?)"
    r"(?: (?:during|in|for|over) the (?:summer|semester|term|school year|internship))?"
)
AVAILABILITY_END = re.compile(
    r"(?:(?:your |my )?availability end date|end (?:date )?of (?:your |my )?availability"
    r"|when does your availability end"
    r"|(?:what is )?(?:the )?(?:last|latest) (?:day|date) (?:you are|you will be|you can be"
    r"|of your) availab\w*"
    r"|(?:until|through) (?:when|what date) (?:are|will) you (?:be )?available"
    r"|(?:what date )?are you available (?:until|through)|available (?:until|through)(?: date)?"
    r"|how long (?:are|will) you (?:be )?available(?: for)?"
    r"|(?:what is your )?(?:(?:preferred|desired|latest|internship|program|term|co ?op) )+end date"
    r"|end of (?:internship|term|program) date|(?:internship|term|program) end"
    r"|(?:what is your )?(?:latest|last) possible end date)"
)
REFERENCES = re.compile(
    r"(?:(?:are |do you have )?(?:professional |work )?references available"
    r"(?: (?:upon|on) request)?"
    r"|(?:can|could|are you able to|are you willing to|will you be able to|would you be able to)"
    r" (?:you )?provide (?:professional |work |us with )*references(?: (?:upon|on|if) request(?:ed)?)?"
    r"|do you have (?:professional |work )?references(?: (?:we|i) (?:can|may|could) contact)?)"
)
BACKGROUND_CHECK = re.compile(
    r"(?:(?:do you |would you |i )?(?:consent|agree|are you willing|are willing|am willing"
    r"|willing)(?: to)? (?:undergo |submit to |complete |participate in |authorize |take"
    r" |a |an |the )*(?:pre employment |pre hire |criminal |standard |routine )*"
    r"background (?:check|checks|screening|screen|investigation)"
    r"(?: (?:and|or|and or) (?:a )?drug (?:test|screen|screening))?"
    r"(?: (?:if|when|as|upon) (?:required|hired|offered employment|a condition of employment"
    r"|offer))?"
    r"|background check (?:consent|authorization|authorisation|acknowledgement|acknowledgment)"
    r"|consent to (?:a )?background check)"
)
ESSENTIAL_FUNCTIONS = re.compile(
    r"(?:are you able to|can you|are you capable of|would you be able to) perform(?:ing)? "
    r"(?:all |each of )?(?:the |all the )?essential (?:functions|duties|job functions"
    r"|responsibilities)(?: of (?:this|the) (?:job|position|role)(?: (?:for which you are"
    r" applying|you are applying for))?)?"
    r"(?: (?:with or without|with|without) (?:a |any )?(?:reasonable )?accommodations?)?"
)
DRIVERS_LICENSE = re.compile(
    r"do you (?:currently )?(?:have|hold|possess) (?:a |an )?(?:valid |current |active"
    r" |unrestricted )*(?:driver s|drivers|driving|driver) licen[cs]e"
    r"(?: and (?:reliable |your own |access to a )?(?:transportation|vehicle|car))?"
)

# The owner's rule for plain questions: yes across the board for willingness and
# acknowledgement questions that are not legal. Each row names a canonical id (or the
# general willingness rule) and the answer; a sensitive or negated question never reaches
# this table.
DEFAULTS = {
    "onsite_willing": "Yes",
    "commute_willing": "Yes",
    "travel_willing": "Yes",
    "available_for_term": "Yes",
    "comfortable_with_stack": "Yes",
    "willingness": "Yes",
    "contact_consent": "Yes",
    "talent_network_opt_in": "Yes",
    "read_job_description": "Yes",
}
# Trivial preference questions: "choose whatever is reasonable".
PREFERENCES = frozenset({"how_did_you_hear"})
# For a dropdown, the first tier with exactly one matching option wins.
HEARD_TIERS = (
    re.compile(r"job ?board|job (?:site|aggregator|search)|online job", re.IGNORECASE),
    re.compile(r"^other\b", re.IGNORECASE),
    re.compile(r"internet|online|web ?site|web search", re.IGNORECASE),
)
HEARD_TEXT = "Online job board"


def _only(words: list[str], need: frozenset | set, allowed: frozenset | set) -> bool:
    return bool(need & set(words)) and set(words) <= allowed


def _strict(name: str, text: str, found: list[str]) -> dict | None:
    """Rules that match a whole label word for word. `text` has place placeholders in."""
    words = text.split()
    if name in EXACT_NAMES:
        canonical = EXACT_NAMES[name]
        return {"id": canonical, "default": canonical if canonical in PREFERENCES else ""}
    match = AUTHORIZED.fullmatch(text)
    if match:
        tail = match.group("tail")
        scope = place_scope(match.group("where") or "", found)
        if WITHOUT_SPONSORSHIP.fullmatch(tail):
            return {"id": "work_authorization_without_sponsorship", "scope": scope}
        if set(tail.split()) <= AUTHORIZED_TAIL:
            return {"id": "work_authorization", "scope": scope}
    condition = LEADING_CONDITION.match(text)
    asked = text[condition.end() :] if condition else text
    body = asked.split()
    if (
        re.match(r"(?:will|do|would) you\b", asked)
        and set(body) <= SPONSORSHIP_WORDS
        and "sponsorship" in body
        and any(v in body[: body.index("sponsorship")] for v in ("require", "need"))
    ):
        now = bool({"now", "currently", "presently", "ever"} & set(body))
        future = bool({"future", "ever"} & set(body))
        tense = "now_or_future" if now == future else ("now" if now else "future")
        # The condition's place counts: "if working in Canada" is the Canadian question.
        return {"id": "sponsorship", "scope": place_scope(text, found), "detail": tense}
    gpa = GPA.fullmatch(text)
    if gpa:
        return {"id": "gpa", "detail": (gpa["scale"] or "").replace(" ", ".")}
    if US_PERSON.search(text) and us_person_asked(text, words):
        return {"id": "us_person"}
    if not sensitive_topic(name):
        # Ties to the employer: one question per employer however a form names it, so
        # the owner's general answer (the warm-up) is found under any wording.
        for canonical, pattern in EMPLOYER_TIES:
            if pattern.fullmatch(text):
                return {"id": canonical, "scope": "employer"}
    if CITIZEN.fullmatch(text):
        return {"id": "citizenship", "scope": place_scope(text, found)}
    if ADULT.fullmatch(text):
        return {"id": "at_least_18"}
    if CLEARANCE.fullmatch(text):
        return {"id": "security_clearance"}
    if FELONY.fullmatch(text):
        return {"id": "criminal_history_felony"}
    if CRIME.fullmatch(text):
        return {"id": "criminal_history"}
    for canonical, need, allowed in VOCABULARY_RULES:
        if _only(words, set(need.split()), set(allowed.split())):
            return {"id": canonical}
    race, ethnicity = {"race", "racial"} & set(words), {"ethnicity", "ethnic"} & set(words)
    if (race or ethnicity) and set(words) <= RACE_WORDS:
        return {"id": "race_ethnicity" if race and ethnicity else "race" if race else "ethnicity"}
    if _only(words, {"certify", "attest", "affirm", "declare", "confirm"}, TRUTHFUL_WORDS) and {
        "true",
        "accurate",
        "correct",
        "truthful",
    } & set(words):
        return {"id": "certify_truthful"}
    private = PRIVACY_OWNER.sub("with ", text).split()
    if (
        _only(private, {"privacy"}, PRIVACY_WORDS)
        and {"policy", "notice", "statement"} & set(private)
        and {"agree", "consent", "acknowledge", "accept", "read", "reviewed", "understand"}
        & set(private)
    ):
        return {"id": "privacy_policy_consent"}
    if _only(words, {"salary", "compensation", "pay", "rate"}, SALARY_WORDS):
        hourly, annual = {"hourly", "hour"} & set(words), {"annual", "yearly", "year"} & set(words)
        unit = "_hourly" if hourly else "_annual" if annual else ""
        return {"id": "salary_expectation" + unit}
    # Acknowledgements the owner's rule covers. Their wording is checked word for word,
    # so they are plain even though "agree" and "consent" are sensitive words elsewhere.
    contact = CONTACT_SENDER.sub("from ", text).split()
    if _only(contact, CONTACT_NOUNS, CONTACT_WORDS) and CONTACT_VERBS & set(contact):
        return {"id": "contact_consent", "sensitivity": PLAIN, "default": "contact_consent"}
    if CONSIDER_OTHER.fullmatch(text) and not {"not", "no", "never", "decline"} & set(words):
        # "Consider me for other roles here too": the talent network by another name.
        return {
            "id": "talent_network_opt_in",
            "sensitivity": PLAIN,
            "default": "talent_network_opt_in",
        }
    if _only(words, {"talent"}, CONTACT_WORDS) and {"network", "community", "pool"} & set(words):
        return {
            "id": "talent_network_opt_in",
            "sensitivity": PLAIN,
            "default": "talent_network_opt_in",
        }
    if _only(words, {"marketing", "newsletter", "newsletters", "promotional"}, MARKETING_WORDS):
        return {"id": "marketing_opt_in", "sensitivity": PLAIN}
    if READ_POSTING.fullmatch(text):
        return {
            "id": "read_job_description",
            "sensitivity": PLAIN,
            "default": "read_job_description",
        }
    if START_DATE.fullmatch(text):
        return {"id": "start_availability"}
    if START_MONTH.fullmatch(text):
        return {"id": "start_month"}
    if text in GRADUATION_DATE or text in GRADUATION_YEAR:
        return {"id": "graduation_date"}
    if LOCATED_IN_US.fullmatch(text):
        return {"id": "located_in_us", "scope": "us"}
    if RELOCATION_HELP.fullmatch(text):
        return {"id": "relocation_assistance_needed"}
    for canonical, pattern in COMMON_RULES:
        if pattern.fullmatch(text):
            return {"id": canonical}
    return None


COMMON_RULES = (
    ("hours_per_week", HOURS_PER_WEEK),
    ("availability_end", AVAILABILITY_END),
    ("references_available", REFERENCES),
    ("background_check_consent", BACKGROUND_CHECK),
    ("essential_functions", ESSENTIAL_FUNCTIONS),
    ("drivers_license", DRIVERS_LICENSE),
)


def _loose(name: str, text: str) -> dict | None:
    """Rules that read the start of a label and tolerate what follows.

    They cover plain questions only: a label that carries any sensitive word is never
    matched here, so a profile fact or a default cannot answer a legal question that
    happens to open like a willingness one.
    """
    if sensitive_topic(name):
        return None
    words = text.split()
    if HEARD.fullmatch(text):
        return {"id": "how_did_you_hear", "default": "how_did_you_hear"}
    if worked_here_employer(text) is not None:
        return {"id": "previously_employed_here", "scope": "employer"}
    enrolled = ENROLLED_AT.fullmatch(text)
    if enrolled and ENROLLED_KIND & set(enrolled["school"].split()):
        # "...enrolled in a bachelor's program?": the owner's to say, never a draft's.
        return {"id": "enrollment_status", "detail": "kind"}
    if enrolled:
        # "Are you currently enrolled at <school>?" asks about that school, today.
        named = [w for w in enrolled["school"].split() if w not in ENROLLED_FILLER]
        return {"id": "enrollment_status", "detail": "at:" + " ".join(named) if named else ""}
    if RETURNING.match(text) and not {"not", "no", "never"} & set(words):
        return {"id": "returning_to_school"}
    if not WILLINGNESS.match(text):
        # Any other question about graduating: answered only by an option that states
        # the approved graduation month.
        return {"id": "graduation_date"} if any(w.startswith("graduat") for w in words) else None
    # A willingness question: which one decides the profile fact or the default.
    risky = "risky" if RISK_WORDS & set(words) else ""
    if RELOCATE.search(text):
        return {"id": "relocate_willing", "detail": risky}
    if any(w.startswith("commut") for w in words):
        canonical = "commute_willing"
    elif ONSITE.search(text):
        canonical = "onsite_willing"
    elif {"travel", "traveling", "travelling"} & set(words):
        canonical = "travel_willing"
    elif STACK.match(text):
        canonical = "comfortable_with_stack"
    elif {"available", "start"} & set(words):
        canonical = "available_for_term"
    elif STATED_WILLINGNESS.match(text) and not US_NAMED.search(text):
        return {"default": "" if risky else "willingness"}
    else:
        return None
    return {"id": canonical, "default": "" if risky else canonical, "detail": risky}


def _needle(name: str) -> dict | None:
    """An identity fact named inside a short label whose other words are all neutral."""
    if len(name.split()) > 6:
        return None
    for needle, canonical in NEEDLES:
        if re.search(rf"\b{needle}\b", name):
            rest = re.sub(rf"\b{needle}\b", " ", name).split()
            return {"id": canonical} if set(rest) <= QUALIFIERS else None
    return None


def _known(name: str, loose: bool) -> dict | None:
    text, found = with_places(name)
    rule = _strict(name, text, found)
    if rule or not loose:
        return rule
    rule = _loose(name, text)
    if rule is None:
        return _needle(name)
    # A loose rule reads past a city, a schedule or a company name. Those words change
    # what an answer means, so the owner's answer is remembered for this wording only.
    return rule if rule.get("id") == "how_did_you_hear" else {**rule, "tolerant": True}


WILLINGNESS_IDS = frozenset(
    {
        "onsite_willing",
        "commute_willing",
        "travel_willing",
        "available_for_term",
        "comfortable_with_stack",
        "relocate_willing",
    }
)


def final_question(label) -> str:
    """The question at the end of a label that opens with statements ("We require all
    employees onsite. Are you able to …?"), as a rule reads it; "" when there is none."""
    text = " ".join(str(label or "").split())
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if len(parts) < 2 or not parts[-1].endswith("?") or any("?" in p for p in parts[:-1]):
        return ""
    return plain_name(parts[-1])


def behind_statement(label, name: str) -> dict | None:
    """A willingness question behind a statement takes the rule of its plain form. What
    the statement says still counts: money words or a place the profile excludes make it
    the owner's (the risk check reads the whole label), and a negation anywhere already
    kept the label from this rule."""
    asked = final_question(label)
    if not asked or asked == name:
        return None
    rule = _known(asked, loose=True)
    if not rule or (rule.get("id") not in WILLINGNESS_IDS and rule.get("default") != "willingness"):
        return None
    if RISK_WORDS & set(with_places(name)[0].split()):
        return {**rule, "default": "", "detail": "risky"}
    return rule


def classify(label, kind: str = "", options=()) -> Question:
    """Canonical identity, class, polarity and scope of one form question."""
    name = plain_name(label)
    bare, negated = affirmative(name)
    # A strict rule may contain its own "without"; everything else sees a negated label
    # only with the negation taken out and marked as the opposite question.
    rule, polarity = _known(name, loose=not negated), POSITIVE
    if rule is None and negated:
        # "Are you unwilling to relocate?" is the relocation question asked the other way
        # round: the same identity, never the same answer.
        rule, polarity = _known(bare, loose=True), NEGATED
    if rule is None and not negated:
        rule = behind_statement(label, name)
    rule = rule or {}
    topic = sensitive_topic(name)
    canonical, scope = rule.get("id", ""), rule.get("scope", "")
    if canonical in {"work_authorization", "sponsorship", "citizenship"} and scope == "us":
        canonical += "_us"
    if rule.get("id") == "sponsorship":
        canonical += "_" + rule["detail"]
    if canonical == "work_authorization_without_sponsorship" and scope == "us":
        canonical = "work_authorization_us_without_sponsorship"
    fingerprint = hashlib.sha256(wording(label).encode()).hexdigest()[:24]
    worded = bool(canonical) and (bool(rule.get("tolerant")) or polarity == NEGATED)
    if not canonical:
        # Export-control wording varies too much for a rule: one id for the class, and
        # each wording stays its own question.
        canonical = "export_control" if topic == "export_control" else "q:" + fingerprint
        worded = topic == "export_control"
        text, found = with_places(name)
        if "xplace" in text.split() and topic:
            scope = "other:" + "+".join(sorted(set(found)))
        elif EMPLOYER_RELATIVE.search(US_COUNTRY_CONTEXT.sub(" ", name)):
            # "Why do you want to work here?" has a different answer at every employer.
            scope = "employer"
    sensitivity = rule.get("sensitivity") or (SENSITIVE if topic else PLAIN)
    if canonical in SENSITIVE_IDS:
        sensitivity = SENSITIVE
    return Question(
        canonical_id=canonical,
        sensitivity=sensitivity,
        polarity=polarity,
        scope=scope,
        known=bool(rule.get("id")),
        topic=topic or TOPIC_OF.get(canonical, ""),
        default=rule.get("default", "") if sensitivity == PLAIN and polarity == POSITIVE else "",
        detail=rule.get("detail", ""),
        name=name,
        variant=fingerprint if worded else "",
        # Nothing to go on: no text, an input's name instead of a question, or the
        # placeholder form reading gives a question it could not read.
        answerable=not form_reading.unreadable({"label": label})
        and bool(name)
        and kind not in {"password", "file", "hidden"},
    )


# Canonical ids that are sensitive whatever words the label happens to use.
TOPIC_OF = {
    "work_authorization": "authorization",
    "work_authorization_us": "authorization",
    "work_authorization_without_sponsorship": "authorization",
    "work_authorization_us_without_sponsorship": "authorization",
    "citizenship": "citizenship",
    "citizenship_us": "citizenship",
    "citizenship_country": "citizenship",
    "us_person": "export_control",
    "at_least_18": "age",
    "security_clearance": "clearance",
    "criminal_history": "criminal",
    "criminal_history_felony": "criminal",
    "gender": "demographics",
    "sex": "demographics",
    "sexual_orientation": "demographics",
    "hispanic_latino": "demographics",
    "race_ethnicity": "demographics",
    "race": "demographics",
    "ethnicity": "demographics",
    "pronouns": "demographics",
    "veteran_status": "veteran",
    "disability_status": "disability",
    "certify_truthful": "agreement",
    "privacy_policy_consent": "agreement",
    "salary_expectation": "salary",
    "salary_expectation_hourly": "salary",
    "salary_expectation_annual": "salary",
    "background_check_consent": "background_check",
    "essential_functions": "disability",
    "drivers_license": "identity_document",
}
SENSITIVE_IDS = frozenset(TOPIC_OF) | {
    f"sponsorship{place}_{tense}"
    for place in ("", "_us")
    for tense in ("now", "future", "now_or_future")
}


def is_sensitive(label, kind: str = "", options=()) -> bool:
    return classify(label, kind, options).sensitivity == SENSITIVE


def is_place_label(label) -> bool:
    return plain_name(label) in PLACE_LABELS


# For a question whose answer depends on the employer. An answer given outside any
# application (in `#memory`) holds for every employer; one given for an application whose
# employer cannot be told is neither kept nor reused.
ANY_EMPLOYER = "*"
NO_EMPLOYER = "?"


def memory_key(question: Question, employer: str = "") -> str | None:
    """What an owner's answer is remembered under: canonical id, polarity and scope.

    A question whose answer depends on the employer is remembered per employer; with no
    employer given the key is the owner's general answer, and with `NO_EMPLOYER` there is
    no key. A question matched by a rule that tolerates extra words ("...relocate to
    Austin?"), and any negated question, is remembered for its own wording: taking a
    negation out loses which negation it was.
    """
    if not question.answerable:
        return None
    scope = question.scope
    if scope == "employer":
        if employer == NO_EMPLOYER:
            return None
        scope = "employer:" + (employer or ANY_EMPLOYER)
    return hashlib.sha256(
        json.dumps([question.canonical_id, question.polarity, scope, question.variant]).encode()
    ).hexdigest()


TENANT_IN_PATH = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com")


def memory_keys(question: Question, employer: str = "") -> list[str]:
    """The keys to look an earlier answer up under: this employer's, then the general one."""
    keys = [memory_key(question, employer)]
    if question.scope == "employer" and employer:
        keys.append(memory_key(question))
    return [key for key in keys if key]


def employer_key(url) -> str:
    """Which employer an application belongs to, for answers that differ per employer.

    `NO_EMPLOYER` when the address does not say: a shared board without its tenant.
    """
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if not host:
        return NO_EMPLOYER
    for suffix in TENANT_IN_PATH:
        if host == suffix or host.endswith("." + suffix):
            tenant = next((p for p in parts.path.split("/") if p), "")
            if tenant in {"", "embed"}:
                tenant = parse_qs(parts.query).get("for", [""])[0]
            return f"{suffix}/{tenant.lower()}" if tenant else NO_EMPLOYER
    return re.sub(r"^(?:www|careers|jobs|apply)\.", "", host)


def ask_each_time(question: Question, profile: dict | None) -> bool:
    """A class the approved profile keeps with the owner on every form.

    Export control is no longer one: the owner asked twice that an answer he gives be
    kept. The profile's `export_control_questions: ask_each_time` now reads "ask once,
    then remember" (still never a default or a draft: the sensitive gate sees to that).
    """
    path = POLICY_KEYS.get(question.topic)
    if not path or question.topic == "export_control":
        return False
    section = (profile or {}).get(path[0]) or {}
    return section.get(path[1], "") == ASK_EACH_TIME


def option_matches(option_label, value) -> bool:
    """Exact option text, the same words run together ("On-site", "Onsite"), or the same
    country written differently."""
    a, b = normalized(option_label), normalized(value)
    return (
        a == b
        or a.replace(" ", "") == b.replace(" ", "")
        or (a in COUNTRY_ALIASES and b in COUNTRY_ALIASES)
    )


def match_option(options, value, *, loose: bool = False) -> str | None:
    """The one offered option that is this value. `loose` also accepts the only option
    that starts with the same yes or no, when the opposite is offered too; a sensitive
    answer is never matched that way. A select's placeholder is never an answer."""
    labels = [str(o) for o in options or [] if not placeholder_option(o)]
    exact = [o for o in labels if option_matches(o, value)]
    if exact:
        return exact[0] if len(exact) == 1 else None
    first = normalized(value).split()[:1]
    if loose and first in (["yes"], ["no"]):
        opposite = ["no"] if first == ["yes"] else ["yes"]
        same = [o for o in labels if normalized(o).split()[:1] == first]
        other = [o for o in labels if normalized(o).split()[:1] == opposite]
        if len(same) == 1 and other:
            return same[0]
    return None


def match_many(options, value) -> list[str] | None:
    """The boxes of a checkbox group an answer ticks, in the form's order.

    Every part of the answer must name an option. A lone box under a question is ticked
    by a plain yes.
    """
    labels = [str(o) for o in options or [] if not placeholder_option(o)]
    chosen = form_reading.match_options(value, labels)
    if chosen is None and len(labels) == 1 and normalized(value) in {"yes", "true"}:
        return labels
    return chosen


def option_labels(options) -> list[str]:
    """The labels of the options offered; a select's placeholder is not one."""
    labels = [str(o.get("label", "")) if isinstance(o, dict) else str(o) for o in options or []]
    return [label for label in labels if not placeholder_option(label)]


# --- a. the approved profile ---------------------------------------------------------
IDENTITY_KEYS = {
    "first_name": "legal_first_name",
    "middle_name": "legal_middle_name",
    "last_name": "legal_last_name",
    "preferred_name": "preferred_name",
    "email": "email",
    "phone": "phone",
    "location_city": "city",
    "state_region": "state_region",
    "country": "country",
    "postal_code": "postal_code",
    "linkedin_url": "linkedin",
    "github_url": "github",
    "portfolio_url": "portfolio",
}


# How forms spell the approved work styles; the first spelling is the one for a text box.
WORK_STYLE_WORDS = {
    "remote": ("Remote", "Fully remote", "Work from home"),
    "hybrid": ("Hybrid",),
    "onsite": ("On-site", "Onsite", "In office", "In-office", "In person", "In-person"),
}


def _yes_no(value) -> list[str] | None:
    return None if value is None else ["Yes" if value else "No"]


def _excluded_place(question: Question, profile: dict) -> bool:
    prefs = profile.get("preferences") or {}
    excluded = [normalized(x) for x in prefs.get("excluded_locations") or [] if x]
    return any(place and f" {place} " in f" {question.name} " for place in excluded)


def profile_fact(
    question: Question, profile: dict, has_options: bool = False, entry: tuple | None = None
):
    """(candidate values, source) from the approved profile, or None.

    A sensitive question is answered here only on an exact positive match of its
    canonical rule; a negated question is never answered from a profile fact. `entry`
    is the (position, school) a form's education block states; without one, a question
    about the school is about the entry applications state (education.py).
    """
    if question.polarity != POSITIVE or not question.known:
        return None
    canonical = question.canonical_id
    identity = profile.get("identity") or {}
    if canonical in IDENTITY_KEYS:
        key = IDENTITY_KEYS[canonical]
        return ([str(identity[key])], "identity." + key) if identity.get(key) else None
    if canonical == "phone_type":
        return ["Mobile"], "default.phone_type"
    if canonical == "full_name":
        parts = [
            identity.get(k)
            for k in ("legal_first_name", "legal_middle_name", "legal_last_name")
            if identity.get(k)
        ]
        return ([" ".join(parts)], "identity.legal_name") if parts else None
    if canonical == "location":
        parts = [identity.get("city"), identity.get("state_region")]
        return ([", ".join(parts)], "identity.location") if all(parts) else None
    if canonical == "full_name_native":
        parts = [
            str(identity[k])
            for k in ("legal_first_name", "legal_middle_name", "legal_last_name")
            if identity.get(k)
        ]
        name = " ".join(parts)
        # A Latin-script legal name is the name in its own script; any other is the owner's.
        return ([name], "identity.legal_name") if parts and latin_script(name) else None
    fact = school_fact(question, profile, has_options, entry)
    if fact is not False:
        return fact
    if canonical == "returning_to_school":
        return returning_fact(profile)
    eligible = profile.get("eligibility") or {}
    if canonical == "work_authorization_us":
        values = _yes_no(eligible.get("us_work_authorized"))
        return (values, "eligibility.us_work_authorized") if values else None
    now, future = eligible.get("sponsorship_now"), eligible.get("sponsorship_future")
    if canonical == "work_authorization_us_without_sponsorship":
        authorized = eligible.get("us_work_authorized")
        if authorized is None or (authorized and (now is None or future is None)):
            return None
        free = bool(authorized) and now is False and future is False
        return _yes_no(free), "eligibility.us_work_authorized+sponsorship"
    if canonical.startswith("sponsorship") and question.scope in {"us", "unspecified"}:
        if question.detail == "now":
            values, source = _yes_no(now), "eligibility.sponsorship_now"
        elif question.detail == "future":
            values, source = _yes_no(future), "eligibility.sponsorship_future"
        else:
            source = "eligibility.sponsorship_now+future"
            if now is True or future is True:
                values = ["Yes"]
            else:
                values = ["No"] if now is False and future is False else None
        return (values, source) if values else None
    if canonical == "citizenship_us":
        values = _yes_no(eligible.get("us_citizen"))
        return (values, "eligibility.us_citizen") if values else None
    if canonical == "us_person":
        # A citizen is a U.S. person by the question's own definition. A non-citizen may
        # still be one (a permanent resident, a refugee): that is the owner's to say.
        return (["Yes"], "eligibility.us_citizen") if eligible.get("us_citizen") is True else None
    if canonical == "at_least_18":
        values = _yes_no(eligible.get("at_least_18"))
        return (values, "eligibility.at_least_18") if values else None
    prefs = profile.get("preferences") or {}
    if canonical == "relocate_willing":
        if question.detail == "risky" or _excluded_place(question, profile):
            # Another country, a cost condition, or a place the owner excluded.
            return None
        values = _yes_no(prefs.get("relocate"))
        return (values, "preferences.relocate") if values else None
    if canonical == "relocation_assistance_needed":
        values = _yes_no(prefs.get("relocation_support_required"))
        return (values, "preferences.relocation_support_required") if values else None
    if canonical == "onsite_willing":
        styles = prefs.get("work_styles") or []
        wanted = {"onsite", "hybrid"} if "hybrid" in question.name.split() else {"onsite"}
        if question.detail == "risky" or _excluded_place(question, profile):
            return None
        return (["Yes"], "preferences.work_styles") if wanted & set(styles) else None
    if canonical == "located_in_us":
        country = normalized(identity.get("country"))
        if not country:
            return None
        return _yes_no(country in COUNTRY_ALIASES), "identity.country"
    availability = profile.get("availability") or {}
    if canonical in {"start_availability", "start_month", "availability_end"}:
        key = "latest_end" if canonical == "availability_end" else "earliest_start"
        day = availability.get(key)
        if not day:
            return None
        when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
        source = "profile.availability." + key
        if canonical == "start_month":
            # The month of the earliest start, with its year; a bare month only from a
            # list of months.
            values = [when.strftime("%B %Y"), when.strftime("%b %Y")]
            if has_options:
                values += [when.strftime("%B"), when.strftime("%b")]
            return values, source
        values = [f"{when.strftime('%B')} {when.day}, {when.year}", day]
        if has_options and canonical == "start_availability":
            values += [when.strftime("%B %Y"), when.strftime("%b %Y")]  # a list of months
        return values, source
    if canonical == "hours_per_week":
        hours = availability.get("hours_per_week")
        return ([str(hours)], "profile.availability.hours_per_week") if hours else None
    if canonical == "preferred_locations":
        places = [str(p) for p in prefs.get("preferred_locations") or [] if p]
        # The joined list for a text box first; one place alone fits an office option.
        return ([", ".join(places), *places], "preferences.preferred_locations") if places else None
    if canonical == "work_style_preference":
        styles = [s for s in prefs.get("work_styles") or [] if s in WORK_STYLE_WORDS]
        if not styles:
            return None
        words = [WORK_STYLE_WORDS[s][0] for s in styles]
        spellings = [spelling for s in styles for spelling in WORK_STYLE_WORDS[s]]
        return [", ".join(words), *spellings], "preferences.work_styles"
    if canonical in {"talent_network_opt_in", "marketing_opt_in"}:
        values = _yes_no((profile.get("application_policy") or {}).get(canonical))
        return (values, "profile.application_policy." + canonical) if values else None
    return None


# Questions about the owner's schools. Which school each one means is education.py's
# call: the entry applications state, the entry a form's block states, the entry he is
# enrolled in today, or the school where a GPA was earned.
SCHOOL_IDS = frozenset(
    {
        "school",
        "major",
        "degree",
        "gpa",
        "graduation_date",
        "degree_start",
        "school_start",
        "current_school",
        "enrollment_status",
        "class_standing",
    }
)
NO_ENTRY = (None, None)  # a form's block with no entry of the owner's to state
# Education facts no model draft answers (draft_gate): code or the owner only.
EDUCATION_FACTS = frozenset(
    {
        "school",
        "degree",
        "major",
        "gpa",
        "graduation_date",
        "school_start",
        "degree_start",
        "current_school",
        "enrollment_status",
        "class_standing",
        "returning_to_school",
    }
)
# Halves of one fact a form asks in two fields (city and state). A model draft never
# fills one half beside the profile's other half (pair_problems).
PAIRED_FACTS = frozenset({"location_city", "state_region"})
ENROLLED_WORDS = (
    "Currently enrolled",
    "Enrolled",
    "Current student",
    "Currently a student",
    "Student",
    "Yes",
)
YEAR_WORDS = {
    "freshman": ("Freshman", "First Year", "1st Year", "Year 1"),
    "sophomore": ("Sophomore", "Second Year", "2nd Year", "Year 2"),
    "junior": ("Junior", "Third Year", "3rd Year", "Year 3"),
    "senior": ("Senior", "Fourth Year", "4th Year", "Year 4"),
}


def month_fact(question: Question, month: str, source: str, has_options: bool):
    """A school month as a form takes it: from a list, a year question, or written out."""
    when = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    if has_options:
        return [when.strftime(f) for f in ("%B %Y", "%b %Y", "%Y")], source
    if question.name in GRADUATION_YEAR:
        return [when.strftime("%Y")], source
    if question.canonical_id != "graduation_date" or question.name in GRADUATION_DATE:
        return [when.strftime("%B %Y")], source
    return None


def returning_fact(profile: dict):
    """He goes back to school after an internship when the graduation applications state
    is in a later year than the internship. Otherwise it is his to say."""
    month = education.graduation(profile)
    if not month or int(month[:4]) <= education.internship_year(""):
        return None
    return ["Yes"], f"education.schools.{education.primary_index(profile)}.graduation_month"


def school_fact(question: Question, profile: dict, has_options: bool, entry: tuple | None):
    """(values, source) for a question about the owner's schools, None when the profile
    cannot say, and False when the question is about something else."""
    canonical = question.canonical_id
    if canonical not in SCHOOL_IDS:
        return False
    if canonical in {"current_school", "school_start", "enrollment_status"}:
        # Where he is enrolled today, whichever school applications state.
        index = education.current_index(profile)
        current = None if index is None else education.schools(profile)[index]
        if canonical == "enrollment_status":
            return enrollment_fact(question, profile, current)
        key = "school" if canonical == "current_school" else "start_month"
        if not current or not current.get(key):
            return None
        source = f"education.schools.{index}.{key}"
        if key == "school":
            return [str(current["school"])], source
        return month_fact(question, current[key], source, has_options)
    if canonical == "class_standing":
        now = education.year_in_school(profile)
        if not now:
            return None
        coming = education.rising(education.graduation(profile), education.internship_year(""))
        values = list(YEAR_WORDS[now]) + ([f"Rising {coming.title()}"] if coming else [])
        return values, f"education.schools.{education.primary_index(profile)}.graduation_month"
    if canonical == "gpa":
        # A GPA belongs to the school where it was earned: a block's own entry, or the
        # one school whose GPA the owner discloses when the form asks for his GPA.
        found = entry if entry is not None else education.disclosed_gpa(profile)
        index, school = found or NO_ENTRY
        if not school or school.get("disclose_gpa") is not True or school.get("gpa") is None:
            return None
        scale = school.get("gpa_scale")
        if question.detail and (scale is None or float(question.detail) != float(scale)):
            return None  # the form asks on another scale than the profile's
        return [str(school["gpa"])], f"education.schools.{index}.gpa"
    stated = entry if entry is not None else (education.ordered(profile)[:1] or [NO_ENTRY])[0]
    index, school = stated
    if not school:
        return None
    if canonical in {"school", "major", "degree"}:
        if not school.get(canonical):
            return None
        return [str(school[canonical])], f"education.schools.{index}.{canonical}"
    key = "graduation_month" if canonical == "graduation_date" else "start_month"
    if not school.get(key):
        return None
    return month_fact(question, school[key], f"education.schools.{index}.{key}", has_options)


def enrollment_fact(question: Question, profile: dict, current: dict | None):
    """Whether he is enrolled now, or enrolled at the school a question names."""
    if question.detail == "kind":
        return None  # which kind of program he is in is not a profile field
    named = question.detail.removeprefix("at:") if question.detail.startswith("at:") else ""
    if not named:
        return (list(ENROLLED_WORDS), "education.enrollment") if current else None

    def words(school) -> list[str]:
        return [w for w in normalized(school).split() if w not in ENROLLED_FILLER]

    if current and words(current.get("school")) == words(named):
        return ["Yes"], "education.enrollment"
    others = [s for s in education.schools(profile) if s is not current]
    if current and any(words(s.get("school")) == words(named) for s in others):
        return ["No"], "education.enrollment"  # a school of his, not the one he attends now
    return None


def latin_script(text: str) -> bool:
    """Every letter is a Latin one (accents included): the name needs no other script."""
    letters = [c for c in str(text or "") if c.isalpha()]
    return bool(letters) and all(unicodedata.name(c, "").startswith("LATIN") for c in letters)


# --- a form's own list for a profile fact ---------------------------------------------
# A form offers its own spelling of the degree and the major. Code maps the approved value
# onto that list, exactly or through a short table; anything else is the owner's pick.
DEGREE_LEVELS = (
    ("doctorate", r"ph ?d|doctor of philosophy|doctorate|doctoral|d phil"),
    ("mba", r"mba|m b a|master of business administration"),
    ("master", r"master\w*|m ?sc?|m ?eng|meng|m ?a|ms[a-z]{0,3}"),
    ("bachelor", r"bachelor\w*|b ?sc?|b ?eng|beng|b ?a|bs[a-z]{0,3}|b ?tech|btech"),
    ("associate", r"associate\w*|a ?a|a ?s|aas"),
)
DEGREE_TYPES = (
    ("science", r"of science|b ?sc?|m ?sc?|bs[a-z]{0,3}|ms[a-z]{0,3}"),
    ("arts", r"of arts|b ?a|m ?a"),
    ("engineering", r"of engineering|b ?eng|beng|m ?eng|meng"),
)
# How boards spell each level and type, most specific first.
DEGREE_SPELLINGS = {
    ("bachelor", "science"): (
        "Bachelor of Science",
        "Bachelor of Science (BS)",
        "Bachelor of Science (B.S.)",
        "Bachelor of Science (BSc)",
        "BS",
        "B.S.",
        "BSc",
        "B.Sc.",
    ),
    ("bachelor", "arts"): ("Bachelor of Arts", "Bachelor of Arts (BA)", "Bachelor of Arts (B.A.)"),
    ("bachelor", "engineering"): ("Bachelor of Engineering", "BEng", "B.Eng."),
    ("bachelor", ""): (
        "Bachelor's Degree",
        "Bachelor's",
        "Bachelors Degree",
        "Bachelor Degree",
        "Bachelor",
        "Undergraduate Degree",
    ),
    ("master", "science"): (
        "Master of Science",
        "Master of Science (MS)",
        "Master of Science (M.S.)",
        "MS",
        "M.S.",
        "MSc",
    ),
    ("master", "arts"): ("Master of Arts", "Master of Arts (MA)", "Master of Arts (M.A.)"),
    ("master", "engineering"): ("Master of Engineering", "MEng", "M.Eng."),
    ("master", ""): ("Master's Degree", "Master's", "Masters Degree", "Master Degree", "Master"),
    ("mba", ""): (
        "Master of Business Administration (M.B.A.)",
        "Master of Business Administration (MBA)",
        "Master of Business Administration",
        "MBA",
        "M.B.A.",
    ),
    ("doctorate", ""): (
        "Doctor of Philosophy (Ph.D.)",
        "Doctor of Philosophy (PhD)",
        "Doctor of Philosophy",
        "PhD",
        "Ph.D.",
        "Doctorate",
        "Doctoral Degree",
        "Doctorate Degree",
    ),
    ("associate", ""): (
        "Associate's Degree",
        "Associate's",
        "Associates Degree",
        "Associate Degree",
        "Associate",
    ),
}


def degree_kind(degree) -> tuple[str, str]:
    """(level, type) of an approved degree: ("bachelor", "science") for "B.S. in CS"."""
    text = normalized(degree)
    level = next((n for n, p in DEGREE_LEVELS if re.search(rf"\b(?:{p})\b", text)), "")
    kind = next((n for n, p in DEGREE_TYPES if re.search(rf"\b(?:{p})\b", text)), "")
    return level, kind if level in {"bachelor", "master"} else ""


def first_listed(labels: list[str], candidates) -> str | None:
    """The option the first candidate names, taking candidates in order; a candidate that
    names two options is ambiguous and ends the search."""
    for candidate in candidates:
        found = [o for o in labels if option_matches(o, candidate)]
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def degree_choice(labels: list[str], degree: str) -> str | None:
    """The board's option for the approved degree: its own words, the same level and type,
    then the level alone ("Bachelor of Science in X" is "Bachelor's Degree")."""
    level, kind = degree_kind(degree)
    if not level:
        return first_listed(labels, [degree])
    spellings = [degree, *DEGREE_SPELLINGS.get((level, kind), ())]
    return first_listed(labels, [*spellings, *DEGREE_SPELLINGS.get((level, ""), ())])


# Majors as boards list them. Only names that mean the same field of study; a near field
# ("Engineering" for "Computer Engineering") is the owner's call, never a guess.
DISCIPLINES = {
    "computer science": (
        "Computer Science",
        "Computer and Information Science",
        "Computer Sciences",
    ),
    "computer sciences": ("Computer Science",),
    "cs": ("Computer Science",),
    "computing": ("Computing", "Computer Science"),
    "computer information science": ("Computer and Information Science", "Computer Science"),
    "computer information sciences": ("Computer and Information Sciences", "Computer Science"),
    "computer science engineering": ("Computer Science and Engineering", "Computer Science"),
    "software engineering": ("Software Engineering", "Computer Science"),
    "computer engineering": ("Computer Engineering",),
    "electrical computer engineering": (
        "Electrical and Computer Engineering",
        "Electrical & Computer Engineering",
    ),
    "electrical engineering": ("Electrical Engineering",),
    "mathematics": ("Mathematics", "Math"),
    "math": ("Mathematics", "Math"),
    "applied mathematics": ("Applied Mathematics", "Mathematics"),
    "applied math": ("Applied Mathematics", "Mathematics"),
    "statistics": ("Statistics",),
    "data science": ("Data Science",),
    "information technology": ("Information Technology", "Information Systems"),
    "information systems": ("Information Systems", "Information Systems Management"),
    "management information systems": (
        "Management Information Systems",
        "Information Systems Management",
        "Information Systems",
    ),
    "cybersecurity": ("Cyber Security", "Cybersecurity"),
    "cyber security": ("Cyber Security", "Cybersecurity"),
    "business administration": ("Business Administration",),
    "economics": ("Economics",),
    "physics": ("Physics",),
    "mechanical engineering": ("Mechanical Engineering",),
    "finance": ("Finance",),
}
DISCIPLINE_FILLER = _words("and of the in")


def squashed(text) -> str:
    return " ".join(w for w in normalized(text).split() if w not in DISCIPLINE_FILLER)


def discipline_choice(labels: list[str], major: str) -> str | None:
    """The board's option for the approved major: the same words, then the table."""
    names = [major, re.split(r"[(,;]| - | with ", str(major))[0].strip()]
    candidates = [*names, *(alias for n in names for alias in DISCIPLINES.get(squashed(n), ()))]
    for candidate in candidates:
        found = [o for o in labels if squashed(o) == squashed(candidate)]
        if len(found) == 1:
            return found[0]
    return None


GPA_RANGE = re.compile(r"(\d(?:\.\d+)?)\s*(?:-|–|to)\s*(\d(?:\.\d+)?)")


def gpa_choice(labels: list[str], gpa: str, scale) -> str | None:
    """The option that is the GPA, or the one 4.0-scale range that holds it."""
    exact = [
        o for o in labels if re.fullmatch(r"\d(?:\.\d+)?", o.strip()) and float(o) == float(gpa)
    ]
    if len(exact) == 1:
        return exact[0]
    if scale is None or float(scale) != 4.0:
        return None
    ranges = []
    for option in labels:
        match = GPA_RANGE.fullmatch(option.strip())
        if match and float(match[1]) <= float(gpa) <= float(match[2]):
            ranges.append(option)
    return ranges[0] if len(ranges) == 1 else None


# --- the education block -----------------------------------------------------------
# Greenhouse words its education dates "Start date month" and "End date year"; a work
# history uses the same words, so the field's own id must say education when the label
# does not.
EDUCATION_DATE = re.compile(
    r"(?:(?:what is your |what s your )?(?:expected|anticipated|estimated|projected) )?"
    r"(?:(?:education|school|degree|college|university) )?"
    r"(?P<edge>start|end|graduation|enrollment)(?: date)? (?P<part>month|year)"
)
EDUCATION_WORDS = _words("education school degree college university graduation enrollment")
EDUCATION_HINT = re.compile(
    r"educat|school|degree|college|universit|(?:start|end)-(?:month|year)--\d", re.IGNORECASE
)
MONTH_YEAR = re.compile(r"([A-Za-z]+)\.? (\d{4})")


def education_part(field: dict) -> tuple[str, str] | None:
    """("start" | "end", "month" | "year") for a date part of a school entry, else None."""
    name = plain_name(field.get("label"))
    match = EDUCATION_DATE.fullmatch(name)
    if not match:
        return None
    named = bool(EDUCATION_WORDS & set(name.split()))
    hinted = EDUCATION_HINT.search(f"{field.get('id') or ''} {field.get('name') or ''}")
    if not (named or hinted):
        return None
    return ("start" if match["edge"] in {"start", "enrollment"} else "end"), match["part"]


def education_date(
    part: tuple[str, str], field: dict, labels: list[str], profile: dict, recall
) -> tuple[str | None, str | None]:
    """A school entry's start or end month or year, from the approved enrollment and
    graduation months of the entry the block states (the first block states the entry
    applications state). A start that lies ahead is given as it is. The start of the
    school he attends now may also be the owner's earlier answer."""
    index, school = education.entry_for_block(profile, education.block_index(field)) or NO_ENTRY
    if school is None:
        return None, None
    edge, unit = part
    key = "start_month" if edge == "start" else "graduation_month"
    month, source = school.get(key), f"education.schools.{index}.{key}"
    attending = index == education.current_index(profile)
    if month:
        when = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    elif edge == "start" and attending and recall is not None:
        said = recall(SCHOOL_START_LABEL, [], kind="", employer="", profile=profile)
        match = MONTH_YEAR.fullmatch(str(said or "").strip())
        try:
            when = datetime.strptime(f"{match[1][:3]} {match[2]}", "%b %Y").replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None, None
        source = REMEMBERED
    else:
        return None, None
    if unit == "year":
        candidates = [str(when.year)]
    else:
        candidates = [
            when.strftime("%B"),
            when.strftime("%b"),
            f"{when.month:02d}",
            str(when.month),
        ]
    if labels:
        chosen = first_listed(labels, candidates)
        if chosen is None and unit == "month":
            chosen = month_choice(labels, when.month)  # "April/May/June" holds May
        return chosen, source
    if unit == "month" and field.get("kind") == "number":
        return str(when.month), source
    if unit == "month" and "mm" in normalized(field.get("placeholder")).split():
        return f"{when.month:02d}", source
    return candidates[0], source


MONTH_NUMBERS = {
    datetime(2000, n, 1, tzinfo=UTC).strftime(f).lower(): n
    for n in range(1, 13)
    for f in ("%B", "%b")
} | {"sept": 9}
MONTH_SPAN = re.compile(r"\b(?:to|through|thru|until)\b|\s[-–—]\s")


def option_months(option: str) -> set[int]:
    """The months an option names: listed ("April/May/June") or a span ("May - August")."""
    text = str(option or "").lower()
    named = [MONTH_NUMBERS[w] for w in re.findall(r"[a-z]+", text) if w in MONTH_NUMBERS]
    if len(named) == 2 and MONTH_SPAN.search(text):
        first, last = named
        span = range(first, last + 1) if first <= last else [*range(first, 13), *range(1, last + 1)]
        return set(span)
    return set(named)


def month_choice(labels: list[str], month: int) -> str | None:
    """The one option whose months hold this month, when the form groups months."""
    found = [o for o in labels if month in option_months(o)]
    return found[0] if len(found) == 1 else None


# --- "have you worked for us before" -------------------------------------------------
SCHOOL_START_LABEL = "When did you start at your current school?"
PAST_EMPLOYERS_LABEL = "Which companies have you worked for, as an employee, intern or contractor?"
NEVER_OPTION = re.compile(r"^no\b|\bnever\b|\bhave not\b|\bnot previously\b")


def employer_names(question: Question, employer: str) -> list[str] | None:
    """The names the employer goes by on this form: the label's, then the address's.
    None when there is none to check, or the label reaches past the employer itself."""
    if re.search(
        r"\b(?:subsidiar\w*|affiliat\w*|parent|partners?|vendors?|clients?)\b", question.name
    ):
        return None
    named = worked_here_employer(question.name)
    names = [" ".join(named)] if named else []
    if employer and employer not in {NO_EMPLOYER, ANY_EMPLOYER}:
        tenant = employer.split("/", 1)[1] if "/" in employer else employer.split(".")[0]
        names.append(normalized(tenant))
    return [n for n in names if n] or None


def mentioned(name: str, text: str) -> bool:
    """Whether the profile's own words name this employer. A name too short to tell
    counts as named: the owner decides."""
    squeezed = name.replace(" ", "")
    if len(squeezed) < 3:
        return True
    return bool(re.search(rf"\b{re.escape(name)}\b", text)) or squeezed in text.replace(" ", "")


def never_worked_here(
    question: Question, labels: list[str], kind: str, profile: dict, employer: str, recall
) -> tuple[str | None, str | None]:
    """The form's "no" or "never" when the employers the owner named do not include this
    one: his approved experience notes, or the list of employers he gave once."""
    if question.canonical_id != "previously_employed_here" or question.polarity != POSITIVE:
        return None, None
    names = employer_names(question, employer)
    if not names:
        return None, None
    evidence = profile.get("evidence") or {}
    notes = [str(n) for n in evidence.get("experience_notes") or [] if n]
    listed = None
    if recall is not None:
        listed = recall(PAST_EMPLOYERS_LABEL, [], kind="", employer="", profile=profile)
    if not notes and listed is None:
        return None, None  # nobody has said who he worked for
    known = {k: profile.get(k) for k in ("education", "evidence", "stories")}
    text = normalized(json.dumps(known, default=str) + " " + str(listed or ""))
    if any(mentioned(name, text) for name in names):
        return None, None  # he named this employer somewhere: his answer, not a rule's
    source = "profile.evidence.experience_notes" if notes else REMEMBERED
    if labels:
        nos = [o for o in labels if NEVER_OPTION.search(normalized(o))]
        return (nos[0], source) if len(nos) == 1 else (None, None)
    return ("No", source) if kind in {"", "text"} else (None, None)


def known_fact(label, profile: dict) -> tuple[str | None, str | None]:
    """A free-text value for this label from the approved profile, with its source."""
    question = classify(label)
    if not question.answerable:
        return None, None
    fact = profile_fact(question, profile)
    return (fact[0][0], fact[1]) if fact else (None, None)


# --- decline, defaults ---------------------------------------------------------------
def decline_self_identification(label, options) -> str | None:
    """Voluntary self-identification is answered with the form's own decline option.

    Declining cannot affect candidacy; it is the standard practice, never an inference
    about the applicant. A form without a decline option stays with the owner.
    """
    if not SELF_ID.search(plain_name(label)):
        return None
    matches = [str(o) for o in options or [] if DECLINE_OPTION.search(str(o))]
    return matches[0] if len(matches) == 1 else None


def policy_default(question: Question, options, profile: dict, kind: str = ""):
    """(value, source) from the owner's standing rules for plain questions, or None."""
    if (
        question.sensitivity != PLAIN
        or question.polarity != POSITIVE
        or not question.answerable
        or not question.default
        or _excluded_place(question, profile)
    ):
        return None
    labels = option_labels(options)
    source = "policy.default." + question.default
    if question.default == "how_did_you_hear":
        if not labels:
            return (HEARD_TEXT, source) if kind in {"", "text", "textarea"} else None
        for tier in HEARD_TIERS:
            matches = [o for o in labels if tier.search(o)]
            if len(matches) == 1:
                return matches[0], source
        return None
    answer = DEFAULTS.get(question.default)
    if answer is None:
        return None
    if question.default == "onsite_willing":
        styles = (profile.get("preferences") or {}).get("work_styles") or []
        if styles and not {"onsite", "hybrid"} & set(styles):
            return None  # the approved profile says remote only: not a yes by default
    if kind == "checkbox_group" and len(labels) == 1:
        return answer, source  # a lone box under the question: yes ticks it
    if labels:
        chosen = match_option(labels, answer, loose=True)
        return (chosen, source) if chosen else None
    # Without options only a checkbox takes a bare yes; a text box wants a sentence.
    return (answer, source) if kind == "checkbox" else None


# --- d. the model, and the gate in front of it ---------------------------------------
def unlabeled(field: dict) -> dict:
    """The marker a pending question carries when the form gave its field no label."""
    return {"label_missing": True} if field.get("label_missing") else {}


def draft_gate(question: dict | None) -> dict | None:
    """Why a model draft may not answer this question, or None when it may.

    `question` is a pending-question or observed-field dict. `words` is for the owner;
    `code` is for the system log.
    """
    if not question:
        return None
    if form_reading.unreadable(question) or not plain_name(question.get("label")):
        return {
            "code": "label_missing",
            "words": "The form's text for this question could not be read, so only you can "
            "say what belongs in it.",
        }
    classified = classify(
        question.get("label"), question.get("kind") or "", option_labels(question.get("options"))
    )
    if classified.sensitivity == SENSITIVE:
        return {
            "code": f"sensitive:{classified.topic or classified.canonical_id}",
            "words": "This is a legal or personal question, so only your own answer is used.",
        }
    education_date = EDUCATION_DATE.fullmatch(plain_name(question.get("label")))
    if education_date or (classified.known and classified.canonical_id in EDUCATION_FACTS):
        # The school, degree, major, GPA and school dates are facts: a list code could
        # not map is the owner's pick, never a model's nearest guess, and a model must
        # never state the wrong school beside the profile's other half of a date. Where
        # he is enrolled and his year in school are facts about today.
        return {
            "code": f"profile_fact:{classified.canonical_id if not education_date else 'date'}",
            "words": "This asks for a fact about your education, so only your profile or "
            "your own answer fills it.",
        }
    if classified.known and classified.canonical_id in PAIRED_FACTS:
        return {
            "code": f"profile_fact:{classified.canonical_id}",
            "words": "This is part of your address, so only your profile or your own answer "
            "fills it.",
        }
    return None


def application_answer(field: dict, answers: dict, automatic: dict) -> dict | None:
    """This application's own answer for a field: the owner's, else what Rove recorded.

    A draft Rove used on its own never stands in for the owner on a sensitive or
    unlabeled question, even if one was stored before the gate existed.
    """
    given = answers.get(field["key"])
    if given:
        return given
    recorded = automatic.get(field["key"])
    if recorded and recorded.get("kind") == "draft" and draft_gate(field):
        return None
    return recorded


def fills_long_text(source) -> bool:
    """A remembered answer or a default may fill a long-text field; a profile fact may not."""
    return source == REMEMBERED or str(source or "").startswith("policy.default.")


# --- the resolver --------------------------------------------------------------------
# Facts a board offers in its own words: the approved value is mapped onto its list.
BY_OPTIONS = frozenset({"degree", "major", "gpa"})
FIRST_PAGE = 100  # Greenhouse's pickers load their lists a hundred at a time


def authorized_as_citizen(labels: list[str], profile: dict) -> str | None:
    """Among several "yes" options that say how he is authorized to work, the one for a
    citizen, when the approved profile says he is one. Any other way is his to pick."""
    yes = [o for o in labels if normalized(o).split()[:1] == ["yes"]]
    if len(yes) < 2 or (profile.get("eligibility") or {}).get("us_citizen") is not True:
        return None
    mine = [
        o
        for o in yes
        if re.search(r"\bcitizens?\b", normalized(o))
        and not re.search(r"\b(?:non|not)\b", normalized(o))
    ]
    return mine[0] if len(mine) == 1 else None


def choose(
    question: Question, labels: list[str], values: list, profile: dict, loose: bool, source=""
):
    """The one offered option a profile fact names, or None."""
    canonical = question.canonical_id
    if canonical == "degree":
        return degree_choice(labels, values[0])
    if canonical == "major":
        return discipline_choice(labels, values[0])
    if canonical == "gpa":
        # The scale of the school the GPA was earned at, named by the fact's source.
        parts = str(source).split(".")
        index = int(parts[2]) if len(parts) > 3 and parts[2].isdigit() else 0
        schools = education.schools(profile)
        scale = schools[index].get("gpa_scale") if index < len(schools) else None
        return gpa_choice(labels, values[0], scale)
    if canonical in {"class_standing", "enrollment_status"}:
        return first_listed(labels, values)  # the closest wording first
    if canonical == "work_authorization_us" and values == ["Yes"]:
        as_citizen = authorized_as_citizen(labels, profile)
        if as_citizen is not None:
            return as_citizen
    chosen = {match_option(labels, v, loose=loose) for v in values} - {None}
    if len(chosen) == 1:
        return chosen.pop()
    return explained_option(labels, values) if not chosen else None


# Words that make a "Yes - …" or "No - …" option say more than yes or no.
EXPLAINED_CONDITIONS = _words(
    "with if only but unless except pending require requires required need needs sponsor"
    " sponsorship visa however until although provided"
)


def explained_option(labels: list[str], values: list) -> str | None:
    """A yes-or-no fact against options that explain themselves ("No - I am not currently
    authorized"): the one option that opens with the fact's word, when every option opens
    with yes or no and that option adds no condition of its own."""
    if len(values) != 1 or normalized(values[0]) not in {"yes", "no"}:
        return None
    opening = [(normalized(o).split() or [""])[0] for o in labels]
    if len(labels) < 2 or not set(opening) <= {"yes", "no"}:
        return None
    mine = [o for o, first in zip(labels, opening, strict=True) if first == normalized(values[0])]
    if len(mine) != 1 or set(normalized(mine[0]).split()) & EXPLAINED_CONDITIONS:
        return None
    return mine[0]


# Questions a form's education block asks once per school.
BLOCK_IDS = frozenset({"school", "degree", "major", "gpa", "graduation_date", "degree_start"})


def education_field(field: dict) -> bool:
    """Whether a field is one of a form's education-block questions, which a form asks
    again for each school (so a repeated one is the next school's, not a twin)."""
    return bool(block_question(field))


# A GPA field that sits in an education block says nothing more than "GPA".
BLOCK_GPA = frozenset({"gpa", "grade point average"})


def block_question(field: dict) -> str:
    """What a field asks within an education block ("school", "start month"), or ""."""
    return _block_question(
        str(field.get("label") or ""),
        str(field.get("kind") or ""),
        str(field.get("id") or ""),
        str(field.get("name") or ""),
    )


@functools.lru_cache(maxsize=4096)
def _block_question(label: str, kind: str, identifier: str, name: str) -> str:
    part = education_part({"label": label, "id": identifier, "name": name})
    if part:
        return " ".join(part)
    question = classify(label, kind)
    if not question.known or question.canonical_id not in BLOCK_IDS:
        return ""
    if question.canonical_id == "gpa" and question.name not in BLOCK_GPA:
        return ""  # "Cumulative GPA" asks for the GPA overall, wherever the form asks it
    return question.canonical_id


def number_education_blocks(fields: list[dict]) -> None:
    """Mark which education block each block question of a page is in, in page order:
    the second "School" on a page is the second block's, however the form names its
    ids. Only the same words asked again start another block ("Graduation date" and
    "Expected graduation" are one question asked twice). Only a page with a block (a
    school, degree, major or school date) is numbered, and a plain "GPA" on it is its
    block's, never the GPA of another school."""
    asked = [(field, block_question(field)) for field in fields]
    if not any(
        what and what not in {"gpa", "graduation_date", "degree_start"} for _, what in asked
    ):
        return
    seen: dict[tuple, int] = {}
    for field, what in asked:
        if what:
            key = (what, plain_name(field.get("label")))
            field["education_block"] = seen.get(key, 0)
            seen[key] = field["education_block"] + 1


def block_entry(field: dict, question: Question, profile: dict) -> tuple | None:
    """The (position, school) a block question states, NO_ENTRY when the block has no
    school of the owner's, None when the field is not in a numbered or repeated block."""
    if not question.known or question.canonical_id not in BLOCK_IDS:
        return None
    index = education.block_index(field)
    if index is None:
        return None
    return education.entry_for_block(profile, index) or NO_ENTRY


def resolve(
    field: dict, profile: dict, *, recall=None, employer: str = "", picker: bool = False
) -> tuple[str | None, str | None]:
    """(value, source) for one observed field from the first source allowed to answer.

    `recall(label, options, kind=, employer=, profile=)` returns the owner's earlier answer.
    `picker` marks a control whose options are not known yet: only values that do not
    depend on the option list are returned, and the caller resolves again once it has
    read the options. Qwen and the owner come after this function, in the worker.
    """
    label, kind = field.get("label") or "", field.get("kind") or ""
    if field.get("label_missing"):
        return None, None
    labels = option_labels(field.get("options"))
    unread = picker and not labels
    part = education_part(field)
    if part:
        value, source = education_date(part, field, labels, profile, recall)
        if value is not None:
            # A picker's list is read first, so the month is matched to its own spelling.
            return (None, None) if unread else (value, source)
    question = classify(label, kind, labels)
    if not question.answerable:
        return None, None
    loose = question.sensitivity == PLAIN
    entry = block_entry(field, question, profile)
    if (part or entry is not None) and education.block_index(field):
        # A later education block states another school: what the owner said for the
        # first never fills it.
        recall = None
    fact = profile_fact(question, profile, bool(labels), entry)
    if fact:
        values, source = fact
        if unread and question.canonical_id in BY_OPTIONS:
            return None, None  # matched to the board's own list: the picker is read first
        if not labels:
            return values[0], source
        chosen = choose(question, labels, values, profile, loose, source)
        if chosen is not None:
            return chosen, source
        if (
            question.canonical_id in {"degree", "major"}
            and field.get("role") == "combobox"
            and len(labels) >= FIRST_PAGE
        ):
            # A long picker list may be only its first page: the picker searches for the
            # approved words and commits only an option that is exactly them.
            return values[0], source
    policy = POLICY_KEYS.get(question.topic)
    section = (profile.get(policy[0]) or {}) if policy else {}
    decline_first = bool(policy) and section.get(policy[1]) == "decline_when_optional"
    declined = None if picker else decline_self_identification(label, labels)
    if declined and decline_first:
        return declined, DECLINED
    if recall is not None and not ask_each_time(question, profile):
        remembered = recall(label, labels, kind=kind, employer=employer, profile=profile)
        if remembered is not None:
            return remembered, REMEMBERED
    if declined:
        return declined, DECLINED
    if picker:
        return None, None
    value, source = never_worked_here(question, labels, kind, profile, employer, recall)
    if value is not None:
        return value, source
    default = policy_default(question, labels, profile, kind)
    return default if default else (None, None)


# --- questions that hang on another --------------------------------------------------
FOLLOW_UP = re.compile(
    r"^(?:if (?:yes|so)\b"
    r"|if you (?:answered|selected|checked|chose|said|replied|responded|indicated) yes\b"
    r"|if your answer (?:is|was) yes\b"
    r"|if (?:the|your) answer to the (?:above|previous|prior) question (?:is|was) yes\b)"
    r"|\b(?:please )?(?:explain|describe|elaborate|specify|provide (?:details|more details))"
    r"(?: [a-z0-9]+){0,4} if (?:yes|so)$"
)


def follow_up(label) -> bool:
    """Whether a question applies only when the one before it was answered yes."""
    return bool(FOLLOW_UP.search(plain_name(label)))


def yes_no_question(field: dict) -> bool:
    """A question answered yes or no: a checkbox, or options that start with both."""
    if field.get("kind") == "checkbox":
        return True
    labels = [normalized(o) for o in option_labels(field.get("options"))]
    return {"yes", "no"} <= {label.split()[0] for label in labels if label}


def said_yes(value) -> bool:
    return (normalized(value).split() or [""])[0] in {"yes", "true"}


def idle_follow_ups(fields: list[dict], filled: list[dict]) -> set[str]:
    """Keys of follow-up questions ("If yes, …") whose question was not answered yes.

    The question a follow-up hangs on is the nearest yes-or-no question before it, in
    form order. Answered no, declined, or not answered at all, the follow-up is left
    blank without a word: never drafted, never asked. Answered yes, it is a question like
    any other.
    """
    answers = {entry.get("key"): entry.get("value") for entry in filled if entry.get("key")}
    idle: set[str] = set()
    parent = None
    for field in fields:
        if field.get("in_group"):
            continue
        if follow_up(field.get("label")):
            if parent is None or not said_yes(answers.get(parent.get("key"))):
                idle.add(str(field.get("key")))
            continue
        if yes_no_question(field):
            parent = field
    return idle


# --- one fact asked in two fields ----------------------------------------------------
PROFILE_RECORDS = ("education.schools.", "identity")


def pair_half(field: dict) -> str | None:
    """The fact a field is half of, when a form asks it in two fields: a school entry's
    graduation or start by month and year, or the city and state."""
    part = education_part(field)
    if part:
        return f"{part[0]}:{education.block_index(field) or 0}"
    question = classify(field.get("label"), field.get("kind") or "")
    return "place" if question.known and question.canonical_id in PAIRED_FACTS else None


def source_entity(source) -> str:
    """Which record a value came from: one school entry, the identity, or anything else
    (an earlier answer, a reply, a draft) as itself."""
    text = str(source or "")
    school = re.match(r"education\.schools\.\d+", text)
    if school:
        return school.group(0)
    return "identity" if text.startswith("identity.") else text


def pair_problems(fields: list[dict], filled: list[dict]) -> list[dict]:
    """Halves of one fact that did not come from one record: the profile's year beside a
    month from anywhere else would be a wrong fact on the application. Each half that
    is not the profile's comes back as a question for the owner, in plain words."""
    by_key = {field.get("key"): field for field in fields}
    facts: dict[str, list] = {}
    for entry in filled:
        field = by_key.get(entry.get("key"))
        fact = pair_half(field) if field else None
        if fact:
            facts.setdefault(fact, []).append(entry)
    problems = []
    for entries in facts.values():
        records = {source_entity(e.get("source")) for e in entries}
        if len(records) < 2 or not any(r.startswith(PROFILE_RECORDS) for r in records):
            continue  # one record, or none of it the profile's (his own answers agree)
        for entry in entries:
            if (
                source_entity(entry.get("source")).startswith(PROFILE_RECORDS)
                and len([r for r in records if r.startswith(PROFILE_RECORDS)]) == 1
            ):
                continue  # the profile's half stands; the other one waits
            problems.append(
                {
                    "label": entry.get("label", ""),
                    "key": entry.get("key"),
                    "required": True,
                    "reason": "This must match the other half of the same date or place, "
                    "which came from your profile; it did not, so it waits for you.",
                }
            )
    return problems


def settled_page(fields: list[dict], filled: list[dict], pending: list[dict]):
    """(filled, pending) for one filled page, after the checks that need the whole page:
    a follow-up whose question was not answered yes is left out without a word, and a
    half of a date or place that does not match the profile's other half waits for the
    owner instead of standing on the form record."""
    idle = idle_follow_ups(fields, filled)
    problems = pair_problems(fields, filled)
    waiting = {problem["key"] for problem in problems}
    pending = [q for q in pending if q.get("key") not in idle and q.get("key") not in waiting]
    return [f for f in filled if f.get("key") not in waiting], pending + problems


# --- preferences among offered options -----------------------------------------------
PREFERENCE = re.compile(
    r"^(?:which|what)\b.*\b(?:interested in|interest you|interests you|prefer|preference"
    r"|preferred|most excited|excite you|like to (?:work|join)|would you like|want to work)\b"
    r"|^(?:areas?|teams?|offices?|locations?|roles?|products?|groups?) of interest\b"
    r"|^preferred (?:team|teams|office|location|group|area|product|track)s?\b"
)
PREFERENCE_RULE = (
    "A preference among the offered options, not a fact: choose the option that best fits "
    "the profile and evidence (any reasonable one is acceptable) and answer with it as a "
    "proposal. Do not answer needs_user for this question."
)


def preference_choice(question: dict) -> bool:
    """A choice of what the owner would like among the form's own options ("Which
    team(s) are you most interested in?") with no fact behind it. The owner's rule for
    such plain questions is to choose whatever is reasonable."""
    labels = option_labels(question.get("options"))
    if len(labels) < 2 or form_reading.unreadable(question):
        return False
    classified = classify(question.get("label"), question.get("kind") or "", labels)
    if classified.sensitivity != PLAIN or classified.known:
        return False  # a fact code knows, or a legal question: never a free pick
    return bool(PREFERENCE.search(classified.name))


def drafting_hints(question: dict) -> dict:
    """What the drafting context adds to one pending question, decided by code."""
    return {"answer_rule": PREFERENCE_RULE} if preference_choice(question) else {}


# --- a school among a picker's suggestions -------------------------------------------
def school_question(field: dict) -> bool:
    """Whether a field asks for a school by name (a school typeahead, when a picker)."""
    question = classify(field.get("label"), field.get("kind") or "")
    return question.known and question.canonical_id in {"school", "current_school"}


SCHOOL_SUFFIX = re.compile(r"\s+[-–—]\s+|,\s+|\s+\(")


def option_name(text) -> str:
    """A suggestion's name: its first line. A picker may show a country, a place or a
    website on the lines under it."""
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def school_option(texts: list[str], school: str, places=()) -> int | None:
    """Which suggestion is the school: its exact name, else its name followed by a campus
    or place ("University of Example - Springfield, IL", "University of Example (UE)").
    With several campuses, the exact name first, then the one whose suffix names one of
    `places` (the owner's city or state). Never the first suggestion for its own sake.
    A suggestion of several lines is its first line; the rest (a country, a website) only
    helps tell campuses apart."""
    wanted = normalized(school)
    if not wanted:
        return None
    names = [option_name(text) for text in texts]
    exact = [i for i, name in enumerate(names) if normalized(name) == wanted]
    if exact:
        return exact[0] if len(exact) == 1 else None
    suffixes = {
        i: parts[1] + " " + " ".join(str(texts[i]).splitlines()[1:])
        for i, name in enumerate(names)
        if len(parts := SCHOOL_SUFFIX.split(name, maxsplit=1)) == 2
        and normalized(parts[0]) == wanted
    }
    if len(suffixes) == 1:
        return next(iter(suffixes))
    named = [normalized(p) for p in places if normalized(p)]
    near = [
        i
        for i, suffix in suffixes.items()
        if any(re.search(rf"\b{re.escape(p)}\b", normalized(suffix)) for p in named)
    ]
    return near[0] if len(near) == 1 else None


def other_option(texts: list[str]) -> int | None:
    """The form's own "Other" choice, when it offers exactly one."""
    found = [
        i
        for i, text in enumerate(texts)
        if re.fullmatch(r"other(?: [a-z ]{0,30})?", normalized(option_name(text)))
    ]
    return found[0] if len(found) == 1 else None
