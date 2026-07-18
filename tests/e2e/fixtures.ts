import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test as base } from "@playwright/test";

const htmxSource = readFileSync(
  resolve(process.cwd(), "node_modules/htmx.org/dist/htmx.min.js"),
  "utf8",
);

export const test = base.extend({
  page: async ({ page }, use) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.route("https://unpkg.com/htmx.org@2.0.4", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/javascript",
        body: htmxSource,
      }),
    );
    await page.route("https://unpkg.com/lucide@latest", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/javascript",
        body: "window.lucide = { createIcons() {} };",
      }),
    );
    await page.route("https://fonts.googleapis.com/**", (route) =>
      route.fulfill({ status: 200, contentType: "text/css", body: "" }),
    );
    await page.route("https://fonts.gstatic.com/**", (route) => route.abort());
    await page.context().route("https://documents.example.test/**", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/pdf",
        body: "%PDF-1.7 e2e fixture",
      }),
    );
    await page.goto("/login");
    await page.getByTestId("login-username-input").fill("e2e-user");
    await page.getByTestId("login-password-input").fill("e2e secure password");
    await page.getByTestId("login-submit-button").click();
    await expect(page).toHaveURL(/\/$/);
    await use(page);
    expect(pageErrors, "aucune erreur JavaScript non gérée").toEqual([]);
  },
});

export { expect };
