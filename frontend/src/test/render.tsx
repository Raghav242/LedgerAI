import { QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { AppProviders } from "@/app/AppProviders";
import { routes } from "@/app/routes";

export function testQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: Infinity }, mutations: { retry: false } },
  });
}

/** Render the whole app (layout + routes) at `path`, with isolated cache and router. */
export function renderApp(path = "/") {
  const client = testQueryClient();
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const user = userEvent.setup();
  const utils = render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { ...utils, user, client, router };
}

/** Render a single component inside the providers (and a router, for <Link>). */
export function renderWithProviders(ui: ReactElement) {
  const client = testQueryClient();
  const router = createMemoryRouter([{ path: "*", element: ui }]);
  const user = userEvent.setup();
  const utils = render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { ...utils, user, client };
}
