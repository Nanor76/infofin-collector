import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

async function createFixtureSearch(
  page: Page,
  options: { markets?: string[]; documentTypes?: string[] } = {},
) {
  const response = await page.request.post("/api/searches", {
    data: {
      markets: options.markets ?? ["Euronext Paris", "Oslo Børs"],
      date_from: "2026-06-01",
      date_to: "2026-06-30",
      document_types: options.documentTypes ?? [],
    },
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as { job_id: string };
}

test("la recherche permet de sélectionner les critères et affiche les résultats", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page.getByTestId("search-heading")).toBeVisible();
  await expect(page.getByTestId("search-map-canvas")).toBeVisible();

  await page.getByTestId("search-market-select-none-button").click();
  await page.getByTestId("search-market-filter-input").fill("Paris");
  const parisOption = page.getByTestId("search-market-option-euronext-paris");
  await expect(parisOption).toBeVisible();
  await parisOption.click();
  await expect(
    page.getByTestId("search-market-checkbox-euronext-paris"),
  ).toBeChecked();

  await page.getByTestId("search-date-from-input").fill("2026-06-01");
  await page.getByTestId("search-date-to-input").fill("2026-06-30");
  await page
    .getByTestId("search-document-type-option-annual-financial-report")
    .click();

  const requestPromise = page.waitForRequest(
    (request) =>
      request.url().endsWith("/api/searches") && request.method() === "POST",
  );
  await page.getByTestId("search-submit-button").click();
  const request = await requestPromise;
  expect(request.postDataJSON()).toMatchObject({
    markets: ["Euronext Paris"],
    date_from: "2026-06-01",
    date_to: "2026-06-30",
    document_types: ["annual_financial_report"],
  });

  await expect(page).toHaveURL(/\/searches\/e2e-[a-f0-9]{32}$/);
  await expect(page.getByTestId("results-job-state")).toHaveText("Terminée");
  await expect(page.getByTestId("results-job-indexed-count")).toHaveText("51");
  await expect(page.getByTestId("results-total-count")).toHaveText("51");
  await expect(page.getByTestId("results-document-row")).toHaveCount(50);
});

test("la sélection rapide, la carte et la validation restent synchronisées", async ({
  page,
}) => {
  await page.goto("/");

  await page.getByTestId("search-market-select-all-button").click();
  await expect(
    page.getByTestId("search-market-checkbox-euronext-paris"),
  ).toBeChecked();
  await expect(page.getByTestId("search-map-country-fr")).toHaveClass(
    /country-selected/,
  );

  await page.getByTestId("search-market-select-none-button").click();
  await expect(
    page.getByTestId("search-market-checkbox-euronext-paris"),
  ).not.toBeChecked();
  await page.getByTestId("search-map-country-fr").click();
  await expect(
    page.getByTestId("search-market-checkbox-euronext-paris"),
  ).toBeChecked();

  await page.getByTestId("search-market-select-none-button").click();
  const dialogPromise = page.waitForEvent("dialog").then(async (dialog) => {
    expect(dialog.message()).toContain("sélectionner au moins un marché");
    await dialog.accept();
  });
  await Promise.all([
    dialogPromise,
    page.getByTestId("search-submit-button").click(),
  ]);
  await expect(page).toHaveURL(/\/$/);
});

test("les filtres HTMX couvrent le type, le texte et l'état vide", async ({
  page,
}) => {
  const search = await createFixtureSearch(page);
  await page.goto(`/searches/${search.job_id}`);

  await page
    .getByTestId("results-filter-document-type-select")
    .selectOption("half_year_financial_report");
  await expect(page.getByTestId("results-total-count")).toHaveText("1");
  const betaRow = page
    .getByTestId("results-document-row")
    .filter({ hasText: "Beta ASA" });
  await expect(betaRow.getByTestId("results-document-title")).toHaveText(
    "Rapport semestriel Beta unique",
  );

  await page
    .getByTestId("results-filter-document-type-select")
    .selectOption("");
  await page.getByTestId("results-filter-query-input").fill("introuvable");
  await expect(page.getByTestId("results-total-count")).toHaveText("0");
  await expect(page.getByTestId("results-empty-state")).toBeVisible();
});

test("le tri, la pagination et l'export CSV sont opérationnels", async ({
  page,
}) => {
  const search = await createFixtureSearch(page);
  await page.goto(`/searches/${search.job_id}`);

  await expect(page.getByTestId("results-current-page")).toHaveText("1");
  await page.getByTestId("results-pagination-next-button").click();
  await expect(page.getByTestId("results-current-page")).toHaveText("2");
  await expect(page.getByTestId("results-document-row")).toHaveCount(1);
  await page.getByTestId("results-pagination-previous-button").click();
  await expect(page.getByTestId("results-current-page")).toHaveText("1");

  await page.getByTestId("results-sort-issuer-name").click();
  const firstIssuer = page
    .getByTestId("results-document-row")
    .first()
    .getByTestId("results-document-issuer-name");
  await expect(firstIssuer).toHaveText("Alpha 00 SA");

  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId("results-export-csv-link").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(`search_${search.job_id}.csv`);
  const stream = await download.createReadStream();
  expect(stream).not.toBeNull();
  let csv = "";
  for await (const chunk of stream!) {
    csv += chunk.toString();
  }
  expect(csv).toContain("market,source,source_document_id");
  expect(csv).toContain("Beta ASA");
});
