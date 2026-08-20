import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, getToken, setToken } from "./api";
import type { User } from "./types";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  login(email: string, password: string): Promise<void>;
  register(email: string, displayName: string, password: string, identity: "member" | "admin"): Promise<RegistrationResult>;
  logout(): void;
}

export interface RegistrationResult {
  status: "active" | "pending_approval";
  message: string;
  user: User;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getToken()) {
      setLoading(false);
      return;
    }
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  async function login(email: string, password: string) {
    const result = await api<{ access_token: string; user: User }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password })
    });
    setToken(result.access_token);
    setUser(result.user);
  }

  async function register(email: string, displayName: string, password: string, identity: "member" | "admin") {
    const result = await api<RegistrationResult & { access_token: string | null }>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, display_name: displayName, password, identity })
    });
    if (result.access_token) {
      setToken(result.access_token);
      setUser(result.user);
    }
    return result;
  }

  function logout() {
    setToken(null);
    setUser(null);
  }

  const value = useMemo(() => ({ user, loading, login, register, logout }), [user, loading]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
