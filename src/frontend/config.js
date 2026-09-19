// The bundled app uses same-origin API calls; the separate Vercel frontend
// points at the public Render backend.
const isVercelHost = window.location.hostname.endsWith("vercel.app");
window.COORDINATOR_BACKEND_URL = isVercelHost
	? "https://ai-ambulance-coordinator-api.onrender.com"
	: "";
