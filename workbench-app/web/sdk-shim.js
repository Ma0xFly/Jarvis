(function () {
  "use strict";
  var React = window.React;
  window.__HERMES_PLUGIN_SDK__ = {
    React: React,
    ReactDOM: window.ReactDOM,
    hooks: {
      useState: React.useState,
      useEffect: React.useEffect,
      useMemo: React.useMemo,
      useCallback: React.useCallback,
      useRef: React.useRef,
      useReducer: React.useReducer,
      useContext: React.useContext
    },
    fetchJSON: async function (url, options) {
      var opts = Object.assign({}, options || {});
      opts.headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
      var res = await fetch(url, opts);
      var text = await res.text();
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
      if (!res.ok) {
        var detail = data && (data.detail || data.error);
        if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg || JSON.stringify(d); }).join("; ");
        throw new Error(detail || ("HTTP " + res.status));
      }
      return data;
    },
    buildWsUrl: function (path) {
      var p = path.charAt(0) === "/" ? path : "/" + path;
      return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + p;
    }
  };

  // The dashboard plugin contract: register(name, component). Standalone just keeps it.
  window.__HERMES_PLUGINS__ = {
    register: function (name, component) {
      window.__WORKBENCH_COMPONENT__ = component;
    }
  };
})();