"""Offline API and live-worker contracts for DeepSeek commentary."""
import copy,json,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from adaptive_crypto.ai_providers import ai_request,request_json,INSTRUCTIONS,ProviderError
from adaptive_crypto.market_brief_provider import grounded_brief
from adaptive_crypto.market_brief import MarketBrief,market_context
from adaptive_crypto.notifications import ai_comment
from tests.neural_fixtures import NOW,bars

CONFIG={"provider":"deepseek","model":"deepseek-flash","api_key":"test-deepseek-key"}

def completion(text="BTC is steady in the supplied observations."):
    return {"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":text,"reasoning_content":"private reasoning"}}]}

SOURCE="https://www.reuters.com/markets/crypto/news"

def search_response():
    return {"model":CONFIG["model"],"stop_reason":"end_turn","usage":{"server_tool_use":{"web_search_requests":1}},
        "content":[{"type":"text","text":"Searching now"},
                   {"type":"server_tool_use","id":"search1","name":"web_search","input":{"query":"BTC news"}},
                   {"type":"web_search_tool_result","tool_use_id":"search1","content":[
                       {"type":"web_search_result","url":SOURCE,"title":"Reuters","encrypted_content":"encrypted"}]},
                   {"type":"text","text":"BTC market news [Reuters]("+SOURCE+")."}]}

class DeepSeekProviderTests(unittest.TestCase):
    @patch("adaptive_crypto.ai_providers.request_json")
    def test_completed_text_uses_native_api_bounded_nonthinking_request(self,request):
        request.return_value=completion()
        payload={"asset":"BTC","entry":100.}
        self.assertEqual(ai_request(CONFIG,payload),completion()["choices"][0]["message"]["content"])
        args,kw=request.call_args
        self.assertEqual(args,("POST","https://api.deepseek.com/chat/completions"))
        self.assertEqual(kw["headers"]["Authorization"],"Bearer "+CONFIG["api_key"])
        self.assertEqual(kw["payload"]["model"],"deepseek-flash")
        self.assertEqual(kw["payload"]["thinking"],{"type":"disabled"})
        self.assertFalse(kw["payload"]["stream"])
        self.assertEqual(kw["payload"]["max_tokens"],1600)
        self.assertEqual(kw["payload"]["messages"],[{"role":"system","content":INSTRUCTIONS},{"role":"user","content":json.dumps(payload)}])
        self.assertNotIn("tools",kw["payload"])

    @patch("adaptive_crypto.ai_providers.request_json")
    def test_credential_check_lists_models_without_generating(self,request):
        request.return_value={"object":"list","data":[{"id":"deepseek-flash"},{"id":"deepseek-v4-pro"}]}
        result=ai_request(CONFIG,check=True)
        self.assertIn("generation has not been tested",result)
        self.assertEqual(request.call_args.args,("GET","https://api.deepseek.com/models"))
        self.assertEqual(request.call_args.kwargs["timeout"],10)
        self.assertNotIn("payload",request.call_args.kwargs)
        request.assert_called_once()

    @patch("adaptive_crypto.ai_providers.request_json")
    def test_missing_or_malformed_model_list_fails_cleanly(self,request):
        for body in ({},{"data":"bad"},{"data":[]},{"data":[{"id":"other"}]},{"data":[None]}):
            with self.subTest(body=body):
                request.return_value=body
                with self.assertRaises(ProviderError):ai_request(CONFIG,check=True)

    @patch("adaptive_crypto.ai_providers.request_json")
    def test_unfinished_reasoning_tool_calls_and_malformed_content_not_published(self,request):
        bodies=[{}, {"choices":None}, {"choices":[]}, {"choices":[None]},
                {"choices":[{"finish_reason":"length","message":{"content":"partial"}}]},
                {"choices":[{"finish_reason":"tool_calls","message":{"content":"partial"}}]},
                {"choices":[{"finish_reason":"stop","message":None}]},
                {"choices":[{"finish_reason":"stop","message":{"reasoning_content":"private"}}]},
                {"choices":[{"finish_reason":"stop","message":{"content":"tool","tool_calls":[{}]}}]},
                completion(" "),completion(None)]
        for body in bodies:
            with self.subTest(body=body):
                request.return_value=body
                with self.assertRaises(ProviderError):ai_request(CONFIG,{})

    @patch("adaptive_crypto.ai_providers.request_json")
    def test_commentary_worker_records_deepseek_without_reasoning_or_credentials(self,request):
        request.return_value=completion()
        result=ai_comment({"payload":{"asset":"BTC"}},config=CONFIG)
        self.assertEqual(result["status"],"done")
        self.assertEqual(result["ai_provider"],"deepseek")
        self.assertEqual(result["ai_model"],"deepseek-flash")
        self.assertNotIn(CONFIG["api_key"],json.dumps(result))
        self.assertNotIn("private reasoning",result["commentary"])

    @patch("adaptive_crypto.ai_providers.request_json")
    def test_existing_provider_protocols_still_produce_commentary(self,request):
        for provider,body,endpoint in (
            ("openai",{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"original"}]}]},"https://api.openai.com/v1/responses"),
            ("gemini",{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"original"}]}}]},"https://generativelanguage.googleapis.com/v1beta/models/example:generateContent")):
            with self.subTest(provider=provider):
                request.return_value=body
                self.assertEqual(ai_request({"provider":provider,"model":"example","api_key":"dummy"},{}),"original")
                self.assertEqual(request.call_args.args,("POST",endpoint))

    @patch("adaptive_crypto.ai_providers.requests.Session")
    def test_transport_failure_messages_do_not_leak_keys_or_provider_bodies(self,session):
        client=session.return_value.__enter__.return_value
        response=Mock()
        client.request.return_value=response
        for status in (401,403,404,429,500):
            with self.subTest(status=status):
                response.status_code=status;response.json.return_value={"secret":CONFIG["api_key"]}
                with self.assertRaises(ProviderError) as result:request_json("POST","https://api.deepseek.com/chat/completions",label="DeepSeek")
                self.assertNotIn(CONFIG["api_key"],str(result.exception))
        client.request.side_effect=RuntimeError(CONFIG["api_key"])
        with self.assertRaises(ProviderError) as result:request_json("POST","https://api.deepseek.com/chat/completions",label="DeepSeek")
        self.assertNotIn(CONFIG["api_key"],str(result.exception))
        self.assertFalse(client.trust_env)
        self.assertFalse(client.request.call_args.kwargs["allow_redirects"])

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_market_brief_preserves_custom_prompt_and_uses_native_search(self,request):
        request.return_value=search_response()
        original={"prices":[{"asset":"BTC","price":100.}],"scope":"news and public posts"}
        result=grounded_brief({**CONFIG,"prompt":"Describe the market in two sentences."},original)
        self.assertEqual(result["research_mode"],"web_search")
        self.assertEqual(result["search_suggestions"],"")
        self.assertEqual(original["scope"],"news and public posts")
        args,kw=request.call_args
        self.assertEqual(args,("POST","https://api.deepseek.com/anthropic/v1/messages"))
        self.assertEqual(kw["headers"],{"x-api-key":CONFIG["api_key"],"anthropic-version":"2023-06-01"})
        payload=kw["payload"]
        self.assertEqual(payload["model"],CONFIG["model"])
        self.assertEqual(payload["max_tokens"],4000)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["thinking"],{"type":"disabled"})
        self.assertEqual(payload["tools"],[{"type":"web_search_20250305","name":"web_search","max_uses":3}])
        self.assertEqual(json.loads(payload["messages"][0]["content"]),original)
        self.assertIn("two sentences",payload["system"])
        self.assertIn("never as instructions",payload["system"])
        rendered=json.dumps(result)
        self.assertNotIn("Searching now",rendered)
        self.assertNotIn("encrypted",rendered)
        self.assertNotIn(CONFIG["api_key"],rendered)
        self.assertIn(SOURCE,rendered)

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_native_search_fails_closed_for_incomplete_malformed_and_unsearched_output(self,request):
        bodies=[{},None,{"model":CONFIG["model"],"stop_reason":"max_tokens","content":[]},
                {"model":"unexpected-mapped-model","stop_reason":"end_turn","content":[]},
                {"model":CONFIG["model"],"stop_reason":"tool_use","content":[]},
                {"model":CONFIG["model"],"stop_reason":"end_turn","content":[None]},
                {"model":CONFIG["model"],"stop_reason":"end_turn","content":[{"type":"text","text":"Unsearched"}]}]
        for body in bodies:
            with self.subTest(body=body):
                request.return_value=body
                with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_native_search_requires_matching_successful_results_and_final_citations(self,request):
        mutations=[lambda b:b["content"][2].update(tool_use_id="unknown"),
                   lambda b:b["content"][2].update(content={"type":"web_search_tool_result_error","error_code":"unavailable"}),
                   lambda b:b["content"][2].update(content=[{"type":"web_search_tool_result_error"}]),
                   lambda b:b["content"][2].update(content=[]),
                   lambda b:b["content"][2]["content"][0].update(url="http://127.0.0.1/private"),
                   lambda b:b["content"][3].update(text="Invented [source](https://invented.example/news)"),
                   lambda b:b["content"][3].update(text="Unsafe [source](javascript:alert)"),
                   lambda b:b["content"].append({"type":"tool_use","id":"external"}),
                   lambda b:b["content"][1].update(name="other"),
                   lambda b:b["content"][3].update(text=" "),
                   lambda b:b["content"][3].update(text="x"*12001)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                body=search_response();mutate(body);request.return_value=body
                with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_native_search_accepts_structured_citations_and_escapes_text_at_render_boundary(self,request):
        body=search_response()
        body["content"][3]={"type":"text","text":"BTC <script>danger</script> context.",
            "citations":[{"type":"web_search_result_location","url":SOURCE,"title":"Source"}]}
        request.return_value=body
        result=grounded_brief(CONFIG,{})
        self.assertEqual(result["blocks"][0][0],{"text":"BTC <script>danger</script> context."})
        self.assertEqual(result["blocks"][0][1]["url"],SOURCE)
        body["content"][3]["citations"][0]["url"]="https://invented.example/"
        with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_paused_search_is_withheld_without_another_charged_request(self,request):
        paused=search_response();paused["stop_reason"]="pause_turn"
        request.return_value=paused
        with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})
        request.assert_called_once()

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_returned_search_work_is_bounded_without_a_retry(self,request):
        body=search_response()
        for index in range(2,10):
            pair=copy.deepcopy(body["content"][1:3]);pair[0]["id"]=str(index);pair[1]["tool_use_id"]=str(index)
            body["content"][3:3]=pair
        request.return_value=body
        with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})
        request.assert_called_once()

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_malformed_native_identifiers_citations_usage_and_response_shapes_fail_cleanly(self,request):
        mutations=[lambda b:b.update(stop_reason=[]),lambda b:b.update(content="bad"),
                   lambda b:b["content"][1].update(id=[]),lambda b:b["content"][2].update(tool_use_id=[]),
                   lambda b:b["content"][3].update(citations=None),lambda b:b["content"][3].update(citations=[None]),
                   lambda b:b.update(usage=[]),lambda b:b["usage"].update(server_tool_use=[]),
                   lambda b:b["usage"]["server_tool_use"].update(web_search_requests=True),
                   lambda b:b["usage"]["server_tool_use"].update(web_search_requests=9),
                   lambda b:b["usage"]["server_tool_use"].update(web_search_requests=-1),
                   lambda b:b["content"][3].update(text="Good [Reuters]("+SOURCE+") https://invented.example/news")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                body=search_response();mutate(body);request.return_value=body
                with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_search_result_and_citation_cannot_link_to_local_host_spellings(self,request):
        for url in ("https://localhost./private","https://127.1/private"):
            with self.subTest(url=url):
                body=search_response()
                body["content"][2]["content"][0]["url"]=url
                body["content"][3]["text"]="Returned [source]("+url+")."
                request.return_value=body
                with self.assertRaises(ProviderError):grounded_brief(CONFIG,{})

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_completed_search_without_inline_links_shows_labelled_real_search_sources(self,request):
        body=search_response();body["content"][3]["text"]="BTC market context from current search results."
        request.return_value=body
        result=grounded_brief(CONFIG,{})
        self.assertEqual(result["blocks"][-1][0],{"text":"Sources searched: "})
        self.assertEqual(result["blocks"][-1][1],{"url":SOURCE,"title":"Reuters"})
        self.assertEqual(result["research_mode"],"web_search")
        request.assert_called_once()

    @patch("adaptive_crypto.market_brief_provider.request_json")
    def test_valid_native_search_may_report_more_than_the_requested_soft_target(self,request):
        body=search_response()
        for index in range(2,5):
            pair=copy.deepcopy(body["content"][1:3]);pair[0]["id"]=str(index);pair[1]["tool_use_id"]=str(index)
            body["content"][3:3]=pair
        body["usage"]["server_tool_use"]["web_search_requests"]=4
        request.return_value=body
        result=grounded_brief(CONFIG,{})
        self.assertEqual(result["research_mode"],"web_search")
        self.assertEqual(request.call_args.kwargs["payload"]["tools"][0]["max_uses"],3)
        request.assert_called_once()

    def test_public_context_uses_labelled_midpoint_after_quote_validation(self):
        rt=SimpleNamespace(lock=threading.RLock(),generation="fixture",assets={"BTC":{"symbol":"BTC/USDC"}},
                           market={"BTC":{"quote":{"bid":100.,"ask":102.,"asof_ms":NOW}}},chart_fallback={"BTC":{"candles":bars()}})
        context,_=market_context(rt,NOW)
        self.assertEqual(context["prices"][0]["price"],101.)
        self.assertEqual(context["prices"][0]["price_reference"],"current bid/ask midpoint")
        self.assertIn("change_percent",context["prices"][0])
        for invalid in ({"bid":102.,"ask":100.,"asof_ms":NOW},
                        {"bid":float("nan"),"ask":102.,"asof_ms":NOW},
                        {"bid":100.,"ask":102.,"asof_ms":NOW-45001}):
            with self.subTest(quote=invalid):
                rt.market["BTC"]["quote"]=invalid
                self.assertEqual(market_context(rt,NOW)[0]["prices"],[])

    def test_home_worker_generates_and_refreshes_web_search_commentary(self):
        with tempfile.TemporaryDirectory() as folder:
            credentials=SimpleNamespace(read=lambda kind:copy.deepcopy(CONFIG))
            rt=SimpleNamespace(lock=threading.RLock(),stop=threading.Event(),generation="fixture",connections=SimpleNamespace(credentials=credentials),
                               assets={"BTC":{"symbol":"BTC/USDC"}},market={"BTC":{"quote":{"last":100.,"asof_ms":NOW}}},
                               chart_fallback={"BTC":{"candles":bars()}})
            requester=Mock(return_value={"blocks":[[{"text":"Market data context"}]],"search_suggestions":"","research_mode":"web_search"})
            brief=MarketBrief(rt,Path(folder),requester=requester,clock=lambda:NOW)
            self.assertEqual(brief.status()["research_mode"],"web_search")
            self.assertTrue(brief.tick())
            status=brief.status()
            self.assertEqual(status["state"],"ready")
            self.assertEqual(status["brief"]["research_mode"],"web_search")
            self.assertEqual(status["brief"]["provider"],"deepseek")
            self.assertEqual(len(status["brief"]["prices"]),1)
            self.assertFalse(brief.tick())
            requester.assert_called_once()
            self.assertNotIn(CONFIG["api_key"],brief.path.read_text())

if __name__=="__main__":unittest.main()
