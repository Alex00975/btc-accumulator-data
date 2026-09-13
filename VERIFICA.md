# VERIFICA DEI SIGILLI — non fidarti, verifica

Ogni giorno la macchina scolpisce su Sepolia l'impronta SHA-256 dei suoi
file (registro + giornale operazioni + catena dei sigilli, concatenati in
quest'ordine). Qui accanto trovi i file ESATTI coperti dall'ultimo
sigillo (cartelle `pubblico/` per BYA Core e `pubblico_one/` per BYA One)
e il loro `manifest.json`.

## Il rito, in cinque passi

1. Scarica i tre file della cartella e il `manifest.json`.
2. Concatenali nell'ordine scritto nel manifesto e calcola la SHA-256.
   Su Mac/Linux: `cat registro.json operazioni.csv sigilli.csv | shasum -a 256`
3. Confronta il risultato con `digest_sha256` nel manifesto.
4. Apri `https://sepolia.etherscan.io/tx/<tx del manifesto>` ->
   "Click to show more" -> "Input Data" -> "View Input As: UTF-8".
5. Verifica che dopo l'etichetta (`SIGILLO1:` / `BYAONE1:`) ci sia la
   STESSA impronta.

Se combacia, hai dimostrato da solo che i numeri pubblicati sono quelli
sigillati quel giorno — senza fidarti di nessuno. Nota: `sigilli.csv`
nello snapshot e' com'era PRIMA della riga dell'ultimo sigillo, perche'
l'impronta copre lo stato precedente alla sua stessa emissione.
