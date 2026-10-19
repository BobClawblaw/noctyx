# Commercial licensing

Noctyx is free for non-commercial use (see [LICENSE](LICENSE)). You need a commercial license if:

- you use Noctyx in production **by or for a for-profit organization** — a company, a startup, a
  consultancy deploying for clients, a lab inside a company — at any size; or
- you offer Noctyx, or what it produces, to others as a **hosted or managed service**.

Evaluation, development and testing never need a license.

A commercial license is an annual subscription priced per managed GPU node, with site licenses for
larger installations and optional support tiers. Pricing is being set during the private beta.

## Paying

Licenses are paid in bitcoin to:

**`bc1q9echf820hapc04y0f636uxdry2ahyx32tkleff`**

1. Open an issue titled "Commercial license" on the project's repository with your organization's
   name and the number of GPU nodes; you get a quote.
2. Pay the quoted amount to the address above.
3. Prove the payment is yours: reply with the transaction id and a **signed message** from the paying
   wallet. Sign this text with the key of one of the transaction's **input** addresses (the address
   you paid from):

   ```
   Noctyx commercial license
   Organization: <legal name>
   GPU nodes: <count>
   Term: <start date> to <end date>
   Payment txid: <txid>
   ```

   Any wallet that signs messages works (BIP-322 for SegWit and Taproot addresses, e.g. Sparrow or
   Electrum; Bitcoin Core's `signmessage` for legacy addresses). Pay from a wallet you control:
   exchanges and custodial wallets cannot sign for the addresses they send from.
4. Once the payment is confirmed and the signature checks out, you receive a signed license file
   naming your organization, the node count and the term; Noctyx shows it as the license state.
   Keep the signed message: it is your proof of purchase if a license is ever questioned.

Non-commercial users: the same address takes donations, and they are what keeps the free tier going.
