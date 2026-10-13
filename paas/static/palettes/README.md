Alternative palettes (no violet). To switch, add after aloof.css in site.head(), dashboard and admin:
  <link rel=stylesheet href="/static/palettes/<name>.css?v=1">
(and add a /static/palettes route), or paste the two blocks over the :root / [data-theme=light] vars in aloof.css.
Tokens: --bg --bg2 --panel --panel2 --line --line2 --fg --fg2 --fg3 --acc --acc2 --acc3 --grad --ok --warn --bad --code
Note: .btn.pri uses --grad; on Ember/Voltage dark, consider color:#0a0807 for button text (light accents).
