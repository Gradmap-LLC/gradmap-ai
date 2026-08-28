"""GradMap MCP server.

A thin translation layer between Claude (via MCP) and portal.gradmap.com/api.
Tool handlers never touch Postgres or decode a JWT to decide trust -- they
forward the student's GradMap access token as a Bearer header and let
GradMap's API be the source of truth. See auth_provider.py for how that
token gets minted in the first place (GradMap has no browser-redirect OAuth
endpoint yet, so this server bridges to its email/password login).

Scope for this first pass: `add_award` only, per gradmap-ai's current
add_award-first rollout plan.
"""

import os
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from auth_provider import GradMapLoginError, GradMapOAuthProvider
from gradmap_client import GradMapAPIError, gradmap_request

# Claude's connector setup fetches this server from Anthropic's own
# infrastructure, not from your browser -- a bare localhost URL (http or
# https) isn't reachable at all. Run this behind a tunnel (e.g. `cloudflared
# tunnel --url http://127.0.0.1:8788`) and set MCP_ISSUER_URL to the public
# https URL the tunnel prints before starting this server; the tunnel
# terminates TLS with a real certificate, so the local hop stays plain HTTP.
ISSUER_URL = os.environ.get("MCP_ISSUER_URL", "http://127.0.0.1:8788")
RESOURCE_SERVER_URL = os.environ.get("MCP_RESOURCE_URL", f"{ISSUER_URL}/mcp")


auth_provider = GradMapOAuthProvider()

server = MCPServer(
    "gradmap",
    title="GradMap",
    instructions=(
        "Helps a high school student manage their GradMap college-admissions profile. "
        "Every tool acts on the profile of whichever student is signed in -- always confirm "
        "ambiguous details with the student rather than guessing them."
    ),
    auth_server_provider=auth_provider,
    auth=AuthSettings(
        issuer_url=ISSUER_URL,
        resource_server_url=RESOURCE_SERVER_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["gradmap"],
            default_scopes=["gradmap"],
        ),
    ),
)


# --- login bridge: GradMap has no browser /authorize endpoint, so we render --
# --- our own form and call POST /auth/login server-side on submit -----------


def _login_page_html(flow_id: str, error: str | None = None) -> str:
    error_html = f'<p style="color:#b00020">{error}</p>' if error else ""
    return f"""
    <!doctype html>
    <html>
      <head><title>Sign in to GradMap</title></head>
      <body style="font-family: sans-serif; max-width: 360px; margin: 80px auto;">
        <h2>Sign in to GradMap</h2>
        <p>Claude needs your GradMap login to act on your behalf.</p>
        {error_html}
        <form method="post" action="{auth_provider.login_path}">
          <input type="hidden" name="flow_id" value="{flow_id}" />
          <label>Email<br/><input type="email" name="email" required style="width:100%" /></label><br/><br/>
          <label>Password<br/><input type="password" name="password" required style="width:100%" /></label><br/><br/>
          <button type="submit">Sign in</button>
        </form>
      </body>
    </html>
    """


@server.custom_route(auth_provider.login_path, methods=["GET"])
async def gradmap_login_page(request: Request) -> Response:
    flow_id = request.query_params.get("flow_id", "")
    if auth_provider.get_pending(flow_id) is None:
        return HTMLResponse(_login_page_html(flow_id, error="This login link has expired. Please reconnect from Claude."), status_code=400)
    return HTMLResponse(_login_page_html(flow_id))


@server.custom_route(auth_provider.login_path, methods=["POST"])
async def gradmap_login_submit(request: Request) -> Response:
    form = await request.form()
    flow_id = str(form.get("flow_id", ""))
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))

    try:
        redirect_uri = await auth_provider.complete_login(flow_id, email, password)
    except GradMapLoginError as error:
        return HTMLResponse(_login_page_html(flow_id, error=error.message), status_code=401)

    return RedirectResponse(url=redirect_uri, status_code=302)


# --- tools -------------------------------------------------------------


def _current_student_id() -> str:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise RuntimeError("Not signed in to GradMap. Please reconnect this MCP server and sign in again.")
    return access_token.subject


@server.tool(
    description=(
        "Show the signed-in student's GradMap dashboard: personalized, prioritized "
        "recommendations (essay planning, course planning, major exploration, financial aid, "
        "upcoming events, letters of recommendation) generated from the student's own profile "
        "and GradMap's knowledge base articles. Call this whenever the student asks what they "
        "should work on next, checks their dashboard, or wants an overview of their progress. "
        "Each recommendation includes an urgency rank, a status, and a link to a supporting "
        "article -- share those links rather than restating the article content yourself. Each "
        "recommendation also includes a google_calendar link (pre-filled with that "
        "recommendation's title and description, no sign-in required) -- when listing "
        "recommendations, show a small calendar icon (\U0001F4C5) next to each one linking to "
        "its google_calendar URL, so the student can add it to their own calendar and pick "
        "their own timing."
    ),
)
async def get_dashboard() -> dict:
    print("[get_dashboard] tool invoked")
    try:
        student_id = _current_student_id()
        access_token = get_access_token()
        print(f"[get_dashboard] student_id={student_id!r}")

        try:
            result = await gradmap_request(
                "POST",
                f"/students/{student_id}/recommendations",
                access_token.token,
            )
        except GradMapAPIError as error:
            raise RuntimeError(f"GradMap couldn't load the dashboard ({error.status_code}): {error.detail}")
    except Exception:
        import traceback

        print("[get_dashboard] FAILED:")
        traceback.print_exc()
        raise

    return result


@server.tool(
    description=(
        "Add a new honor or award to the signed-in student's GradMap profile. "
        "Before calling this tool, ask the student directly for the honor's exact title, "
        "whether it is 'Academic' or 'Non-academic' (honor_type), which grade level(s) they "
        "received it in (grade_levels), and how wide the recognition was: school, state, "
        "national, or international (recognition_level). Never guess honor_type or "
        "recognition_level -- a wrong guess misrepresents the honor on the student's college "
        "applications. action_to_achieve and eligibility_requirements are optional; only fill "
        "them in if the student volunteers that detail. Leave the include_in_*_app flags at "
        "their default (true, included everywhere) unless the student says they don't want "
        "this honor listed on a specific application."
    )
)
async def add_award(
    honor_title: str,
    honor_type: Literal["Academic", "Non-academic"],
    recognition_level: Literal["school", "state", "national", "international"],
    grade_levels: list[Literal["9", "10", "11", "12", "post_graduate"]],
    action_to_achieve: str | None = None,
    eligibility_requirements: str | None = None,
    include_in_common_app: bool = True,
    include_in_uc_app: bool = True,
    include_in_csu_app: bool = True,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/honors",
            access_token.token,
            json={
                "honor_title": honor_title,
                "honor_type": honor_type,
                "recognition_level": recognition_level,
                "grade_levels": grade_levels,
                "action_to_achieve": action_to_achieve,
                "eligibility_requirements": eligibility_requirements,
                "include_in_common_app": include_in_common_app,
                "include_in_uc_app": include_in_uc_app,
                "include_in_csu_app": include_in_csu_app,
            },
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't save this award ({error.status_code}): {error.detail}")

    return {"added": True, "honor": result}


@server.tool(
    description=(
        "Add a new extracurricular activity, club, volunteer work, job, or academic program to "
        "the signed-in student's GradMap profile. Before calling this tool, ask the student "
        "directly for: the organization/program name, what category it falls under (e.g. "
        "'Academic Activity', 'Club/Organization', 'Volunteer Work', 'Work', 'Sports' -- ask "
        "which fits best rather than guessing), the closest UC application category (e.g. "
        "'Educational Prep', 'Extracurricular Activity', 'Volunteer/Community Service', 'Work "
        "Experience'), their role/title, whether it was a leadership role, which grade level(s) "
        "they participated in, when during the year it happens (during the school year, during "
        "a break, or all year), hours per week, weeks per year, and a short description of what "
        "they did. Never guess category, category_uc, activity_type, grade_levels, or timing -- "
        "these materially affect how the activity reads on a college application. If the "
        "activity is a paid job, also ask for the employer description and set is_paid_work to "
        "true; start_date/end_date/hours_per_week_low/hours_per_week_high only apply to paid "
        "work and should otherwise be left blank."
    )
)
async def add_activity(
    program_name: str,
    category: str,
    category_uc: str,
    activity_type: str,
    position_description: str,
    is_leadership_role: bool,
    grade_levels: list[Literal["9", "10", "11", "12", "post_graduate"]],
    timing: Literal["during_school_year", "during_break", "all_year"],
    hours_per_week: float,
    weeks_per_year: float,
    description: str,
    is_currently_participating: bool = True,
    intends_to_continue: bool = False,
    notable_distinctions: str | None = None,
    is_paid_work: bool = False,
    organization_description: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    hours_per_week_low: float | None = None,
    hours_per_week_high: float | None = None,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/activities",
            access_token.token,
            json={
                "program_name": program_name,
                "category": category,
                "category_uc": category_uc,
                "activity_type": activity_type,
                "position_description": position_description,
                "is_leadership_role": is_leadership_role,
                "grade_levels": grade_levels,
                "timing": timing,
                "hours_per_week": hours_per_week,
                "weeks_per_year": weeks_per_year,
                "description": description,
                "is_currently_participating": is_currently_participating,
                "intends_to_continue": intends_to_continue,
                "notable_distinctions": notable_distinctions,
                "is_paid_work": is_paid_work,
                "organization_description": organization_description,
                "start_date": start_date,
                "end_date": end_date,
                "hours_per_week_low": hours_per_week_low,
                "hours_per_week_high": hours_per_week_high,
            },
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't save this activity ({error.status_code}): {error.detail}")

    return {"added": True, "activity": result}


@server.tool(
    description=(
        "Add a custom task to the signed-in student's GradMap dashboard, separate from the "
        "AI-generated recommendations. Ask the student for the task title. category and "
        "urgency_rank are optional labels -- only set them if the student explicitly wants the "
        "task categorized or prioritized; otherwise leave them unset rather than guessing one."
    )
)
async def add_recommendation(
    title: str,
    subtext: str | None = None,
    link: str | None = None,
    category: Literal[
        "essay_planning",
        "course_planning",
        "major",
        "financial_aid",
        "upcoming_events",
        "letters_of_recommendation",
    ]
    | None = None,
    urgency_rank: Literal["due_soon", "coming_up", "later"] | None = None,
    estimated_time: str | None = None,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/recommendations/custom",
            access_token.token,
            json={
                "title": title,
                "subtext": subtext,
                "link": link,
                "category": category,
                "urgency_rank": urgency_rank,
                "estimated_time": estimated_time,
            },
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't add that task ({error.status_code}): {error.detail}")

    return {"added": True, "recommendation": result}


@server.tool(
    description=(
        "Remove a recommendation or task from the signed-in student's GradMap dashboard. Before "
        "calling this, make sure you've shown the student their current list (e.g. via "
        "get_dashboard) and they've explicitly confirmed which one to remove by its title. Use "
        "the exact numeric recommendation_id from that list -- never guess or infer an id."
    )
)
async def remove_recommendation(recommendation_id: int) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "DELETE",
            f"/students/{student_id}/recommendations/{recommendation_id}",
            access_token.token,
        )
    except GradMapAPIError as error:
        if error.status_code == 404:
            raise RuntimeError(
                f"No recommendation with id {recommendation_id} was found for this student. "
                "Double-check the id from a recent get_dashboard call."
            )
        raise RuntimeError(f"GradMap couldn't remove that recommendation ({error.status_code}): {error.detail}")

    return result


if __name__ == "__main__":
    # DNS-rebinding protection only trusts the Host header values listed here.
    # Behind a tunnel, requests arrive with Host: <tunnel-domain> (no port), so
    # that domain -- derived from ISSUER_URL -- has to be allowed explicitly,
    # on top of the plain localhost access used for direct local testing.
    issuer_host = urlparse(ISSUER_URL).netloc
    allowed_hosts = ["127.0.0.1:*", "localhost:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
    if issuer_host and issuer_host not in allowed_hosts:
        allowed_hosts.append(issuer_host)
        allowed_origins.append(ISSUER_URL)

    server.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8788")),
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
    )
