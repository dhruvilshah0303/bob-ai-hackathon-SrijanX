// Frontend route guard + role->portal redirect. This is convenience only -
// the backend enforces every permission independently via require_role()
// (see auth_service.py); a user blocked here could still be blocked (or
// not) by the API directly, and the API's answer is the one that counts.
const ROLE_HOME = {
  ADMIN: "dispatcher.html",
  DISPATCHER: "dispatcher.html",
  HOSPITAL_ADMIN: "hospital.html",
  AMBULANCE_OPERATOR: "ambulance.html",
};

function redirectToOwnPortal(user) {
  window.location.href = ROLE_HOME[user.role] || "login.html";
}

// Call at the top of every protected page. Redirects to /login.html if no
// session exists locally; also redirects away if the logged-in role isn't
// one this page is built for (e.g. an AMBULANCE_OPERATOR hitting
// dispatcher.html directly by URL).
async function requireRole(...allowedRoles) {
  const stored = Api.getStoredUser();
  if (!stored) {
    window.location.href = "login.html";
    return null;
  }
  try {
    const user = await Api.getCurrentUser(); // re-validates the token against the DB (is_active, etc.), not just trusting localStorage
    if (allowedRoles.length && !allowedRoles.includes(user.role)) {
      redirectToOwnPortal(user);
      return null;
    }
    return user;
  } catch (e) {
    Api.clearSession();
    window.location.href = "login.html";
    return null;
  }
}

async function handleLogout() {
  await Api.logout();
  window.location.href = "login.html";
}

window.Auth = { requireRole, redirectToOwnPortal, handleLogout, ROLE_HOME };
