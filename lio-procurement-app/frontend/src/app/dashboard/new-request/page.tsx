'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useMutation } from '@tanstack/react-query';
import * as api from '@/lib/api';
import { useCommodityGroups } from '@/lib/hooks/useCommodityGroups';
import { Button, Input, Alert, Loader } from '@/components/base';

interface OrderLine {
  positionDescription: string;
  unitPrice: number;
  quotedUnitPrice?: number;
  currency?: string;
  catalogArticleId?: string;
  catalogArticleNumber?: string;
  amount: number;
  unit: string;
  totalPrice: number;
}

function calculateDraftTotal(lines: OrderLine[], adjustments: api.ExtractionAdjustment[]): number {
  return lines.reduce((sum, line) => sum + line.totalPrice, 0)
    + adjustments.reduce((sum, adjustment) => sum + adjustment.amount, 0);
}

function formatMoney(value: number, currency: string | undefined): string {
  // Currency is optional when a requester starts a manual request or when a
  // quote does not state one. Keep the amount useful without inventing a
  // currency symbol; the currency is still shown when it is known.
  if (!currency) {
    return value.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(value);
  } catch {
    return `${currency} ${value.toFixed(2)}`;
  }
}

function unitsCompatible(quotedUnit: string, catalogUnit: string): boolean {
  const aliases: Record<string, string> = {
    ea: 'piece',
    each: 'piece',
    pcs: 'piece',
    pc: 'piece',
    piece: 'piece',
    pieces: 'piece',
    license: 'license',
    licenses: 'license',
  };
  const normalize = (unit: string) => aliases[unit.trim().toLowerCase()] || unit.trim().toLowerCase();
  return normalize(quotedUnit) === normalize(catalogUnit);
}

export default function NewRequestPage() {
  const router = useRouter();
  const { data: commodityGroups } = useCommodityGroups();

  // Form state
  const [requestorName, setRequestorName] = useState('');
  const [title, setTitle] = useState('');
  const [vendorName, setVendorName] = useState('');
  const [vatId, setVatId] = useState('');
  const [department, setDepartment] = useState('');
  const [commodityGroupId, setCommodityGroupId] = useState<number | null>(null);
  const [quoteCurrency, setQuoteCurrency] = useState<string | null>(null);
  const [overrideConflict, setOverrideConflict] = useState(false);
  const [overrideGroupIds, setOverrideGroupIds] = useState<number[]>([]);
  const [overrideApplied, setOverrideApplied] = useState(false);
  const [overrideReason, setOverrideReason] = useState('');
  const [orderLines, setOrderLines] = useState<OrderLine[]>([]);
  const [adjustments, setAdjustments] = useState<api.ExtractionAdjustment[]>([]);
  const [alternatives, setAlternatives] = useState<api.ExtractionAdjustment[]>([]);
  const [totalCost, setTotalCost] = useState<number>(0);

  // UI state
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [warning, setWarning] = useState('');
  const [extracting, setExtracting] = useState(false);
  const [catalogSuggestions, setCatalogSuggestions] = useState<Record<number, api.ArticleSuggestion[]>>({});
  const [loadingSuggestions, setLoadingSuggestions] = useState<Record<number, boolean>>({});

  // Search the organization's negotiated catalog as descriptions become available.
  // Suggestions are advisory: the requester still chooses whether to apply one.
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const loadSuggestions = async () => {
      const descriptions = orderLines.map((line) => line.positionDescription.trim());
      const eligible = descriptions
        .map((description, index) => ({ description, index }))
        .filter(({ description }) => description.length >= 2);
      if (eligible.length === 0) {
        setCatalogSuggestions({});
        setLoadingSuggestions({});
        return;
      }

      setLoadingSuggestions(Object.fromEntries(eligible.map(({ index }) => [index, true])));
      const results: Array<readonly [number, api.ArticleSuggestion[]]> = [];
      // Avoid creating one backend fuzzy-search request per line at once.
      // Four concurrent searches keep the form responsive for mixed requests.
      for (let offset = 0; offset < eligible.length; offset += 4) {
        const batch = eligible.slice(offset, offset + 4);
        const batchResults: Array<[number, api.ArticleSuggestion[]]> = await Promise.all(
          batch.map(async ({ description, index }): Promise<[number, api.ArticleSuggestion[]]> => {
            try {
              return [index, await api.articles.suggestions(description, 3, controller.signal)];
            } catch {
              return [index, []];
            }
          }),
        );
        results.push(...batchResults);
        if (controller.signal.aborted) return;
      }
      if (cancelled) return;
      setCatalogSuggestions(Object.fromEntries(results));
      setLoadingSuggestions({});
    };

    const timeout = setTimeout(loadSuggestions, 300);
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timeout);
    };
  }, [orderLines.map((line) => line.positionDescription).join('|')]);

  // PDF parsing mutation
  const parsePDFMutation = useMutation({
    mutationFn: async (file: File) => {
      const result = await api.pdf.parse(file);
      if (!result.text) {
        throw new Error('Failed to parse PDF');
      }
      return result.text;
    },
  });

  // Handle file upload and extraction
  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const uploadedFile = e.target.files?.[0];
    if (!uploadedFile) return;

    if (uploadedFile.type !== 'application/pdf') {
      setError('Please upload a PDF file');
      return;
    }

    setFile(uploadedFile);
    setError('');
    setSuccess('');
    setWarning('');
    setExtracting(true);

    try {
      // Step 1: Parse PDF
      const pdfText = await parsePDFMutation.mutateAsync(uploadedFile);

      // Step 2: Extract structured data AND classify commodity group in one AI call
      const extractionResult = await api.extraction.extract(pdfText);
      
      if (!extractionResult.success || !extractionResult.data) {
        throw new Error(extractionResult.error || 'Failed to extract data');
      }

      const extracted = extractionResult.data;

      // Step 3: Auto-fill form (including commodity group from extraction)
      setTitle(extracted.title || '');
      setVendorName(extracted.vendorName || '');
      setVatId(extracted.vatId || '');
      setDepartment(extracted.department || '');
      setQuoteCurrency(extracted.currency || null);
      setOverrideConflict(Boolean(extractionResult.overrideConflict));
      setOverrideGroupIds(extractionResult.overrideGroupIds || []);
      setOverrideApplied(Boolean(extractionResult.overrideApplied));
      setOverrideReason(extractionResult.overrideReason || '');
      setOrderLines(
        (extracted.orderLines || []).map((line) => ({
          ...line,
          quotedUnitPrice: line.unitPrice,
          currency: extracted.currency || undefined,
        })),
      );
      setAdjustments(extracted.adjustments || []);
      setAlternatives(extracted.alternatives || []);
      setTotalCost(extracted.totalCost ?? calculateDraftTotal(
        (extracted.orderLines || []).map((line) => ({
          ...line,
          quotedUnitPrice: line.unitPrice,
          currency: extracted.currency || undefined,
        })),
        extracted.adjustments || [],
      ));
      
      // Set commodity group (now included in extraction)
      if (extracted.commodityGroupId) {
        setCommodityGroupId(extracted.commodityGroupId);
      }

      // Show both missing-field prompts and deterministic reconciliation warnings.
      const extractionWarnings = [
        ...(extractionResult.missingFields || []).map((field) => `Could not extract: ${field}.`),
        ...(extractionResult.warnings || []),
      ];
      if (extractionWarnings.length > 0) {
        setWarning(
          `⚠️ ${extractionWarnings.join(' ')} Please review the extracted values before submitting.`
        );
      }

      setSuccess('✓ PDF extracted successfully! Review and submit.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err) {
      console.error('Extraction error:', err);
      setError(err instanceof Error ? err.message : 'Failed to extract data from PDF');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } finally {
      setExtracting(false);
    }
  };

  // Add order line manually
  const addOrderLine = () => {
    setOrderLines([
      ...orderLines,
      {
        positionDescription: '',
        unitPrice: 0,
        amount: 1,
        unit: '',
        totalPrice: 0,
      },
    ]);
  };

  // Update order line
  const updateOrderLine = (index: number, field: keyof OrderLine, value: string | number) => {
    const updated = [...orderLines];
    updated[index] = { ...updated[index], [field]: value };

    // A catalog reference is only valid while the quoted line still describes
    // the same unit, description, and applied price.
    if (
      updated[index].catalogArticleId
      && (field === 'positionDescription' || field === 'unit' || field === 'unitPrice')
    ) {
      delete updated[index].catalogArticleId;
      delete updated[index].catalogArticleNumber;
    }
    
    // Auto-calculate total price
    if (field === 'unitPrice' || field === 'amount') {
      updated[index].totalPrice = updated[index].unitPrice * updated[index].amount;
    }
    if (field === 'unitPrice') {
      updated[index].quotedUnitPrice = Number(value);
    }
    
    setOrderLines(updated);
    
    // Update total cost
    const newTotal = calculateDraftTotal(updated, adjustments);
    setTotalCost(newTotal);
  };

  // Remove order line
  const removeOrderLine = (index: number) => {
    const updated = orderLines.filter((_, i) => i !== index);
    setOrderLines(updated);
    const newTotal = calculateDraftTotal(updated, adjustments);
    setTotalCost(newTotal);
  };

  const applyCatalogSuggestion = (index: number, article: api.ArticleSuggestion) => {
    const updated = [...orderLines];
    const line = updated[index];
    const unitPrice = Number(article.unit_price);
    updated[index] = {
      ...line,
      unitPrice,
      unit: article.unit,
      catalogArticleId: article.id,
      catalogArticleNumber: article.article_number,
      totalPrice: unitPrice * line.amount,
    };
    setOrderLines(updated);
    setTotalCost(calculateDraftTotal(updated, adjustments));
  };

  // Submit form
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccess('');
    setWarning('');

    // Validation with scroll to top on error
    if (!commodityGroupId) {
      setError('Please select a commodity group');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    if (overrideConflict) {
      setError('Resolve the commodity-group conflict before submitting this request.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    if (orderLines.length === 0) {
      setError('Please add at least one order line');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    try {
      const created = await api.requests.create({
        requestorName,
        titleShortDescription: title,
        vendorName,
        vatId,
        commodityGroupId: commodityGroupId,
        totalCost,
        currency: quoteCurrency,
        department,
        orderLines,
        adjustments,
      });

      // Attach the original PDF (if one was uploaded) so it shows on the detail page.
      if (file) {
        try {
          await api.requests.uploadDocument(created.id, file);
        } catch {
          // Non-fatal — the request itself was created.
        }
      }

      setSuccess('Request created successfully! Redirecting...');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(() => {
        router.push('/dashboard/requests');
        router.refresh();
      }, 1500);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create request');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  return (
    <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-semibold tracking-tight text-ink">New Procurement Request</h1>
        <p className="text-gray-600 mt-2">
          Upload a vendor offer PDF or fill in the details manually
        </p>
      </div>

      {error && <Alert variant="error" className="mb-6">{error}</Alert>}
      {warning && <Alert variant="warning" className="mb-6">{warning}</Alert>}
      {overrideApplied && overrideReason && (
        <Alert variant="info" className="mb-6">
          {overrideReason}. The commodity group was set by your organization rule.
        </Alert>
      )}
      {overrideConflict && (
        <Alert variant="warning" className="mb-6">
          Multiple organization rules matched. Select one of:{' '}
          {overrideGroupIds
            .map((id) => commodityGroups?.find((group) => group.id === id)?.name || `Group ${id}`)
            .join(', ')}.
        </Alert>
      )}
      {success && <Alert variant="success" className="mb-6">{success}</Alert>}

      <form onSubmit={handleSubmit} className="space-y-8">
        {/* PDF Upload Section */}
        <div className="lio-card p-6">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Upload Vendor Offer (Optional)</h2>
          <p className="text-sm text-gray-600 mb-4">
            Upload a PDF to automatically extract vendor details and order items
          </p>

          <div className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-accent-deep/50 transition-colors">
            <input
              type="file"
              accept="application/pdf"
              onChange={handleFileChange}
              className="hidden"
              id="pdf-upload"
              disabled={extracting}
            />
            <label htmlFor="pdf-upload" className="cursor-pointer">
              {extracting ? (
                <div className="flex flex-col items-center">
                  <Loader size="lg" />
                  <p className="mt-4 text-sm text-gray-600">Extracting data from PDF...</p>
                </div>
              ) : file ? (
                <div>
                  <svg
                    className="mx-auto h-12 w-12 text-green-500"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M5 13l4 4L19 7"
                    />
                  </svg>
                  <p className="mt-2 text-sm font-medium text-gray-900">{file.name}</p>
                  <p className="mt-1 text-xs text-gray-500">Click to upload a different file</p>
                </div>
              ) : (
                <div>
                  <svg
                    className="mx-auto h-12 w-12 text-gray-400"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
                    />
                  </svg>
                  <p className="mt-2 text-sm text-gray-600">
                    Click to upload or drag and drop
                  </p>
                  <p className="mt-1 text-xs text-gray-500">PDF up to 10MB</p>
                </div>
              )}
            </label>
          </div>
        </div>

        {/* Request Details */}
        <div className="lio-card p-6 space-y-4">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Request Details</h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input
              label="Requestor Name *"
              value={requestorName}
              onChange={(e) => setRequestorName(e.target.value)}
              required
              placeholder="John Doe"
            />

            <Input
              label="Department"
              value={department}
              onChange={(e) => setDepartment(e.target.value)}
              placeholder="IT Department"
            />
          </div>

          <Input
            label="Title / Short Description *"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            placeholder="Adobe Creative Cloud Subscription"
          />
        </div>

        {/* Vendor Information */}
        <div className="lio-card p-6 space-y-4">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Vendor Information</h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input
              label="Vendor Name *"
              value={vendorName}
              onChange={(e) => setVendorName(e.target.value)}
              required
              placeholder="Adobe Systems"
            />

            <Input
              label="Tax ID"
              value={vatId}
              onChange={(e) => setVatId(e.target.value)}
              placeholder="12-3456789"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Commodity Group *
            </label>
            <select
              value={commodityGroupId || ''}
              onChange={(e) => {
                const selectedId = Number(e.target.value);
                setCommodityGroupId(selectedId);
                if (overrideGroupIds.includes(selectedId)) setOverrideConflict(false);
              }}
              required
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-gray-900 focus:ring-2 focus:ring-accent-deep/60 focus:border-accent-deep/40"
            >
              <option value="">
                {commodityGroups ? 'Select a commodity group' : 'Loading...'}
              </option>
              {commodityGroups?.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.id} - {group.category} - {group.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Order Lines */}
        <div className="lio-card p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold tracking-tight text-ink">Order Lines *</h2>
            <Button type="button" onClick={addOrderLine} variant="outline" size="sm">
              + Add Line
            </Button>
          </div>

          {orderLines.length === 0 ? (
            <div className="text-center py-8 text-gray-500">
              No order lines yet. Add one to get started.
            </div>
          ) : (
            <div className="space-y-4">
              {orderLines.map((line, index) => (
                <div key={index} className="border border-gray-200 rounded-lg p-4">
                  <div className="flex items-start justify-between mb-4">
                    <h3 className="text-sm font-medium text-gray-700">Line {index + 1}</h3>
                    <button
                      type="button"
                      onClick={() => removeOrderLine(index)}
                      className="text-red-600 hover:text-red-700 text-sm"
                    >
                      Remove
                    </button>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
                    <div className="lg:col-span-2">
                      <Input
                        label="Description"
                        value={line.positionDescription}
                        onChange={(e) =>
                          updateOrderLine(index, 'positionDescription', e.target.value)
                        }
                        required
                        placeholder="Item description"
                      />
                    </div>

                      <Input
                        label={`Unit Price${quoteCurrency ? ` (${quoteCurrency})` : ''}`}
                      type="number"
                      step="0.01"
                      value={line.unitPrice}
                      onChange={(e) =>
                        updateOrderLine(index, 'unitPrice', parseFloat(e.target.value) || 0)
                      }
                      required
                    />

                    <Input
                      label="Amount"
                      type="number"
                      step="0.01"
                      value={line.amount}
                      onChange={(e) =>
                        updateOrderLine(index, 'amount', parseFloat(e.target.value) || 1)
                      }
                      required
                    />

                    <Input
                      label="Unit"
                      value={line.unit}
                      onChange={(e) => updateOrderLine(index, 'unit', e.target.value)}
                      required
                      placeholder="pcs, licenses, etc."
                    />
                  </div>

                  <div className="mt-2 text-right">
                    <span className="text-sm text-gray-600">Total: </span>
                    <span className="text-lg font-semibold tracking-tight text-ink">
                      {formatMoney(line.totalPrice, quoteCurrency || line.currency)}
                    </span>
                  </div>

                  {(loadingSuggestions[index] || catalogSuggestions[index]?.length > 0) && (
                    <div className="mt-4 rounded-md bg-accent-soft/20 p-3">
                      <p className="text-xs font-semibold uppercase tracking-wide text-accent-deep">
                        Negotiated catalog suggestions
                      </p>
                      {loadingSuggestions[index] ? (
                        <p className="mt-1 text-sm text-gray-600">Searching your catalog…</p>
                      ) : (
                        <div className="mt-2 space-y-2">
                          {catalogSuggestions[index]?.map((article) => (
                            <div
                              key={article.id}
                              className="flex flex-col gap-2 rounded border border-accent/30 bg-white p-2 sm:flex-row sm:items-center sm:justify-between"
                            >
                              <div className="text-sm text-gray-700">
                                <span className="font-medium">{article.description}</span>
                                <span className="ml-2 text-gray-500">
                                  {article.supplier_name || 'Catalog supplier'} · {article.currency} {Number(article.unit_price).toFixed(2)} / {article.unit} · catalog qty {Number(article.quantity)} (informational) · match {(article.match_score * 100).toFixed(0)}%
                                </span>
                                <span className="block text-xs text-gray-500">
                                  Quote: {formatMoney(line.quotedUnitPrice ?? line.unitPrice, quoteCurrency || line.currency)} · Catalog: {formatMoney(Number(article.unit_price), article.currency)} · {quoteCurrency && quoteCurrency.toUpperCase() === article.currency.toUpperCase()
                                    ? ((line.quotedUnitPrice ?? line.unitPrice) >= Number(article.unit_price)
                                      ? `Savings: ${formatMoney((line.quotedUnitPrice ?? line.unitPrice) - Number(article.unit_price), article.currency)} / unit`
                                      : `Price increase: ${formatMoney(Number(article.unit_price) - (line.quotedUnitPrice ?? line.unitPrice), article.currency)} / unit`)
                                    : 'Savings unavailable across currencies'}
                                </span>
                                {article.supplier_name && vendorName && article.supplier_name.toLowerCase() !== vendorName.toLowerCase() && (
                                  <span className="block text-xs text-amber-700">
                                    Different supplier: catalog agreement is with {article.supplier_name}; request vendor remains {vendorName}.
                                  </span>
                                )}
                                {!unitsCompatible(line.unit, article.unit) && (
                                  <span className="block text-xs text-amber-700">
                                    Unit mismatch: quoted {line.unit}, catalog {article.unit}. Review pack size before applying.
                                  </span>
                                )}
                              </div>
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                disabled={Boolean(
                                  (quoteCurrency && quoteCurrency.toUpperCase() !== article.currency.toUpperCase())
                                  || !unitsCompatible(line.unit, article.unit),
                                )}
                                onClick={() => applyCatalogSuggestion(index, article)}
                              >
                                {quoteCurrency && quoteCurrency.toUpperCase() !== article.currency.toUpperCase()
                                  ? 'Currency mismatch'
                                  : !unitsCompatible(line.unit, article.unit)
                                    ? 'Unit mismatch'
                                  : 'Use catalog price'}
                              </Button>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {adjustments.length > 0 && (
          <div className="lio-card p-6">
            <h2 className="text-xl font-semibold tracking-tight text-ink">Quote Adjustments</h2>
            <p className="text-sm text-gray-600 mt-1 mb-4">
              Shipping, fees, taxes, and discounts are shown from the quote and included in the total.
            </p>
            <div className="space-y-2">
              {adjustments.map((adjustment, index) => (
                <div key={`${adjustment.description}-${index}`} className="flex items-center justify-between border-b border-line py-2 text-sm">
                  <span className="text-gray-700">{adjustment.description || adjustment.lineType}</span>
                  <span className="font-medium text-ink">
                    {formatMoney(adjustment.amount, quoteCurrency || undefined)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        {alternatives.length > 0 && (
          <div className="lio-card p-6 mt-4">
            <h2 className="text-lg font-semibold tracking-tight text-ink mb-1">Excluded Alternatives</h2>
            <p className="text-sm text-ink/55 mb-3">Optional alternatives are shown for review and are not included in the total.</p>
            <div className="space-y-2">
              {alternatives.map((alternative, index) => (
                <div key={`${alternative.description}-${index}`} className="flex justify-between bg-ink-800/[0.02] rounded-lg p-3 text-sm">
                  <span>{alternative.description || 'Alternative'}</span>
                  <span className="font-medium">{formatMoney(alternative.amount, quoteCurrency || undefined)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Total Cost */}
        <div className="bg-accent-soft/20 border-2 border-accent/40 rounded-lg p-6">
          <div className="flex items-center justify-between">
            <span className="text-lg font-medium text-gray-900">Total Cost</span>
            <span className="text-3xl font-bold text-accent-deep">
              {formatMoney(totalCost, quoteCurrency || undefined)}
            </span>
          </div>
        </div>

        {/* Submit */}
        <div className="flex items-center justify-end space-x-4">
          <Button
            type="button"
            variant="outline"
            onClick={() => router.back()}
          >
            Cancel
          </Button>
          <Button type="submit" size="lg">
            Create Request
          </Button>
        </div>
      </form>
    </div>
  );
}

