// Dumps the HTML of the farside.co.uk pages with flows into Bitcoin and Ethereum ETFs to the files btc-etf.html and eth-etf.html
// in the directory given as the first argument (default ./out). The site is protected by Cloudflare, so a plain HTTP request is
// not enough: we use a browser (Playwright, headless Chromium).
//
//   node scripts/scrape-etf.js out/
//
// Each page is tried several times. Exit code 0 when BOTH pages were saved; 1 when either is missing (the workflow then stops
// publishing, and the data published earlier stays unchanged). Note: Cloudflare may block data-center addresses, including the
// GitHub-hosted Actions runners; in that case run the workflow on your own runner (see the README, the ETF_RUNNER variable).
const fs = require( 'fs' );
const path = require( 'path' );
const { chromium } = require( 'playwright' );

const PAGES = [
  { url: 'https://farside.co.uk/bitcoin-etf-flow-all-data/', file: 'btc-etf.html' },
  { url: 'https://farside.co.uk/ethereum-etf-flow-all-data/', file: 'eth-etf.html' },
];
const ATTEMPTS = 3;
const RETRY_DELAY_MS = 20000;
const CONTEXT_OPTIONS = {
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
  ignoreHTTPSErrors: true,
  locale: 'pl-PL', timezoneId: 'Europe/Warsaw',
  viewport: { width: 1920, height: 1080 },
};

const sleep = ( ms ) => new Promise( ( resolve ) => setTimeout( resolve, ms ) );

async function fetchPage( browser, url, target ) {
  const context = await browser.newContext( CONTEXT_OPTIONS );
  const page = await context.newPage();
  try {
    const resp = await page.goto( url, { waitUntil: 'load', timeout: 60000 } );
    if( !resp || !resp.ok() ) { throw new Error( `HTTP ${resp ? resp.status() : 'no response'}` ); }
    await page.waitForSelector( 'table', { timeout: 30000 } );
    const html = await page.content();
    if( !html.includes( '<table' ) || /Just a moment|cf_chl/.test( html ) ) { throw new Error( 'this is not a data page (a Cloudflare challenge or an empty page)' ); }
    const tmp = `${target}.tmp`;   // atomic write
    fs.writeFileSync( tmp, html );
    fs.renameSync( tmp, target );
    return html.length;
  } finally {
    await page.close().catch( () => {} );
    await context.close().catch( () => {} );
  }
}

async function main() {
  const out = path.resolve( process.argv[2] || 'out' );
  fs.mkdirSync( out, { recursive: true } );
  const browser = await chromium.launch( { headless: true } );
  let failed = 0;
  try {
    for( const { url, file } of PAGES ) {
      let done = false;
      for( let attempt = 1; attempt <= ATTEMPTS && !done; attempt += 1 ) {
        try {
          const len = await fetchPage( browser, url, path.join( out, file ) );
          console.log( `saved ${file} (${len} characters)` );
          done = true;
        } catch( err ) {
          console.error( `attempt ${attempt}/${ATTEMPTS}: could not fetch ${url}: ${err.message}` );
          if( attempt < ATTEMPTS ) { await sleep( RETRY_DELAY_MS ); }
        }
      }
      if( !done ) { failed += 1; console.log( `::error::could not fetch ${url}` ); }
    }
  } finally {
    await browser.close();
  }
  process.exitCode = failed ? 1 : 0;
}

main().catch( ( err ) => { console.error( err ); process.exitCode = 1; } );
