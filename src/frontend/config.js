// Set this to the public Render backend URL ONLY when deploying this
// frontend separately (e.g. to Vercel) from the backend it talks to.
// Empty string = same-origin - correct for local development
// (`uvicorn app:app`, which serves this file itself) and the bundled
// single-container deployment. Get this wrong locally and every API call
// silently goes to whatever URL is hardcoded here instead of your local
// server - which, if that's a live production backend, fails in a
// confusing way (a POST to a path that server doesn't have falls through
// to its static-file mount, which only serves GET/HEAD, so you see
// "405 Method Not Allowed" instead of a clear "not found").
window.COORDINATOR_BACKEND_URL = "";
