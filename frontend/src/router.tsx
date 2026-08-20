import type { AnchorHTMLAttributes } from "react";
import { Link, Redirect, useLocation, useParams } from "wouter";

export { Link, useParams };

export function Navigate({ to, replace = false }: { to: string; replace?: boolean }) {
  return <Redirect to={to} replace={replace} />;
}

export function useNavigate() {
  const [, navigate] = useLocation();
  return navigate;
}

type NavLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  to: string;
  end?: boolean;
};

export function NavLink({ className, end = false, to, ...props }: NavLinkProps) {
  const [location] = useLocation();
  const exact = end || to === "/";
  const active = exact
    ? location === to
    : location === to || location.startsWith(`${to}/`);
  const classes = [className, active ? "active" : ""].filter(Boolean).join(" ");

  return <Link {...props} to={to} className={classes || undefined} />;
}
