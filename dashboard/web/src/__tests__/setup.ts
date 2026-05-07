import '@testing-library/jest-dom';

// Default fetch stub — tests that need real endpoints stub their own.
// Without this, components that mount + fire useFetch in beforeEach throw
// noisy "fetch is not defined" warnings under happy-dom.
if (typeof globalThis.fetch === 'undefined') {
  globalThis.fetch = async () => {
    return new Response(JSON.stringify({}), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  };
}

// happy-dom stubs sessionStorage / localStorage but not window.scrollTo.
if (typeof Element !== 'undefined') {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = function () {};
  }
}
