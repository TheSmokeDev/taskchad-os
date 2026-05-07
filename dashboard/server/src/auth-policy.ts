/**
 * Auth policy boot snapshot (R5 Minor 3 / R4 NM1 — PRP §1232-1241).
 *
 * 4-branch token policy evaluated AT STARTUP only:
 *   (a) Both DASHBOARD_TOKEN and ORCHESTRATION_API_TOKEN set + EQUAL → start normally.
 *   (b) Both set + UNEQUAL → REJECT, exit 1.
 *   (c) Exactly one set → treat as alias for the other, start normally.
 *   (d) NEITHER set → behavior depends on bind:
 *       - Bind non-loopback → REJECT, exit 1.
 *       - Bind 127.0.0.1 + DASHBOARD_DEV_MODE_NO_AUTH unset → REJECT, exit 1.
 *       - Bind 127.0.0.1 + DASHBOARD_DEV_MODE_NO_AUTH=true → start with WARN-on-every-request.
 *
 * Once the policy is captured into a module-scope const at boot, subsequent
 * runtime mutation of those env vars does NOT change request-time auth
 * behavior. Auth is matched against the boot-time snapshot.
 *
 * This is a deliberate boot-snapshot policy (operator intent captured once;
 * rotation requires a server restart) and is NOT a Rule 2 stale-cache
 * violation — see PRP §1241.
 */

export type AuthPolicyMode = 'token-equal' | 'token-alias' | 'dev-mode-loopback';

export interface AuthPolicy {
  mode: AuthPolicyMode;
  expectedToken: string | null;
  warnPerRequest: boolean;
  bind: string;
}

export interface ResolveResult {
  policy: AuthPolicy | null;
  error: string | null;
}

/**
 * Evaluate the 4-branch token policy from environment variables.
 * Returns either a valid policy or a descriptive error message.
 *
 * Pure function — does not read process.env directly. Caller passes in
 * the env values so callers (including tests) can supply explicit values.
 */
export function resolveAuthPolicy(env: {
  dashboardToken: string | undefined;
  orchestrationApiToken: string | undefined;
  devModeNoAuth: string | undefined;
  bind: string;
}): ResolveResult {
  const { dashboardToken, orchestrationApiToken, devModeNoAuth, bind } = env;

  const dashSet = !!dashboardToken && dashboardToken.length > 0;
  const orchSet = !!orchestrationApiToken && orchestrationApiToken.length > 0;
  const isLoopback = bind === '127.0.0.1' || bind === '::1' || bind === 'localhost';

  // Branch (a) — both equal.
  if (dashSet && orchSet && dashboardToken === orchestrationApiToken) {
    return {
      policy: {
        mode: 'token-equal',
        expectedToken: dashboardToken!,
        warnPerRequest: false,
        bind,
      },
      error: null,
    };
  }

  // Branch (b) — both unequal.
  if (dashSet && orchSet && dashboardToken !== orchestrationApiToken) {
    return {
      policy: null,
      error:
        'DASHBOARD_TOKEN and ORCHESTRATION_API_TOKEN are both set but DIFFER. ' +
        'Aborting startup — set them to the same value or unset one (it will be aliased).',
    };
  }

  // Branch (c) — exactly one set; alias for the other.
  if (dashSet !== orchSet) {
    const aliased = dashSet ? dashboardToken! : orchestrationApiToken!;
    return {
      policy: {
        mode: 'token-alias',
        expectedToken: aliased,
        warnPerRequest: false,
        bind,
      },
      error: null,
    };
  }

  // Branch (d) — neither set.
  if (!isLoopback) {
    return {
      policy: null,
      error:
        'No token configured and bind is non-loopback — set ORCHESTRATION_API_TOKEN ' +
        'or DASHBOARD_TOKEN before exposing the dashboard server.',
    };
  }

  if (devModeNoAuth !== 'true') {
    return {
      policy: null,
      error:
        'No token configured; loopback bind alone does NOT enable no-auth — ' +
        'set ORCHESTRATION_API_TOKEN or set DASHBOARD_DEV_MODE_NO_AUTH=true ' +
        'to explicitly opt into dev mode.',
    };
  }

  // Loopback + explicit dev-mode opt-in.
  return {
    policy: {
      mode: 'dev-mode-loopback',
      expectedToken: null,
      warnPerRequest: true,
      bind,
    },
    error: null,
  };
}

/**
 * Captured at server boot in index.ts; null until set. Auth middleware
 * matches against this snapshot, never against process.env.
 *
 * Module-scope mutable state is INTENTIONAL here — see PRP §1241 for why
 * this is a deliberate boot-snapshot and is documented as NOT a Rule 2
 * violation.
 */
let _AUTH_POLICY: AuthPolicy | null = null;

export function setAuthPolicy(policy: AuthPolicy): void {
  _AUTH_POLICY = policy;
}

export function getAuthPolicy(): AuthPolicy | null {
  return _AUTH_POLICY;
}

/**
 * Test-only: clear the snapshot so a subsequent test can resolve fresh.
 */
export function _resetAuthPolicyForTest(): void {
  _AUTH_POLICY = null;
}
