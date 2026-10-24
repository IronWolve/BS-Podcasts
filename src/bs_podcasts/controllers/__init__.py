"""Application-lifetime controllers: the business logic that used to live in the main window.

A controller owns state and policy for one concern (feed refresh, downloads, Discover, and backups, update
checks and database maintenance), survives the theme-driven window rebuild, and never imports a widget. The window
binds to it as its view: it asks through methods and properties, and is told what to show through a small callback
interface the controller parks while no window is bound."""
