"""Testes de normalização textual do onboarding discovery."""
from __future__ import annotations

from app.utils.onboarding_text_normalization import (
    extract_user_terms_normalized,
    is_onboarding_stopword,
    normalize_word_cloud_term,
    sanitize_user_message,
)


class TestSanitizeUserMessage:
    def test_removes_email_and_truncates(self):
        long_tail = "x" * 300
        raw = f"Meu email é joao@example.com e quero frete {long_tail}"
        sanitized = sanitize_user_message(raw, max_length=200)
        assert "joao@example.com" not in sanitized
        assert "[email]" in sanitized
        assert len(sanitized) <= 200

    def test_removes_phone_and_cpf(self):
        raw = "Ligue 11 98765-4321 ou envie cpf 123.456.789-00 sobre frete"
        sanitized = sanitize_user_message(raw)
        assert "98765" not in sanitized
        assert "123.456.789" not in sanitized
        assert "[telefone]" in sanitized
        assert "[cpf]" in sanitized


class TestExtractUserTermsNormalized:
    def test_removes_stopwords_keeps_useful_terms(self):
        terms = extract_user_terms_normalized(
            "Quero reduzir o custo de frete com BI para logística"
        )
        assert "frete" in terms
        assert "custo" in terms
        assert "bi" in terms
        assert "logistica" in terms
        assert "quero" not in terms
        assert "o" not in terms

    def test_normalizes_accents_and_punctuation(self):
        terms = extract_user_terms_normalized("Preciso de cotação e previsão de prazo!")
        assert "cotacao" in terms
        assert "previsao" in terms
        assert "prazo" in terms
        assert "preciso" not in terms

    def test_deduplicates_terms(self):
        terms = extract_user_terms_normalized("frete frete custo custo")
        assert terms.count("frete") == 1
        assert terms.count("custo") == 1

    def test_expanded_comparison_terms(self):
        terms = extract_user_terms_normalized(
            "Quero fazer um BID e equalização de propostas com tarifas comparativo entre transportadoras"
        )
        assert "bid" in terms
        assert "propostas" in terms
        assert "tarifas" in terms
        assert "comparativo" in terms
        assert "transportadoras" in terms
        assert "equalizacao" in terms


class TestNormalizeWordCloudTerm:
    def test_normalizes_accents_and_case(self):
        assert normalize_word_cloud_term("Olá") == "ola"

    def test_rejects_empty_or_short(self):
        assert normalize_word_cloud_term("") == ""
        assert normalize_word_cloud_term("a") == ""
